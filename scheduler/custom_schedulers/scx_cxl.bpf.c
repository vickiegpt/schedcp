/* SPDX-License-Identifier: GPL-2.0 */
/*
 * CXL PMU-aware scheduler with DAMON integration for MoE VectorDB workloads
 * 
 * This scheduler integrates CXL PMU metrics with DAMON for real-time memory
 * access pattern monitoring, optimizing scheduling for MoE VectorDB and
 * implementing intelligent kworker promotion/demotion.
 *
 * Enhanced with bandwidth-aware scheduling for read/write intensive workloads.
 *
 * Features:
 * - Real-time DAMON memory access pattern monitoring
 * - CXL PMU metrics for memory bandwidth/latency optimization
 * - MoE VectorDB workload-aware scheduling
 * - Dynamic kworker promotion/demotion based on memory patterns
 * - Bandwidth-aware scheduling for read/write intensive tasks
 * - Token bucket algorithm for bandwidth control
 */

#include <scx/common.bpf.h>

char _license[] SEC("license") = "GPL";

#define MAX_CPUS 1024
#define MAX_TASKS 8192
#define DAMON_SAMPLE_INTERVAL_NS (100 * 1000 * 1000)
#define MOE_VECTORDB_THRESHOLD 80
#define KWORKER_PROMOTION_THRESHOLD 70
#define BANDWIDTH_THRESHOLD_MB 70
#define FALLBACK_DSQ_ID 0
#define READ_INTENSIVE_DSQ_ID 1
#define WRITE_INTENSIVE_DSQ_ID 2
#define VECTORDB_DSQ_ID 3
#define TOKEN_BUCKET_REFILL_INTERVAL_NS (10 * 1000 * 1000)
#define MB_TO_BYTES(mb) ((mb) * 1024 * 1024)

UEI_DEFINE(uei);

const volatile u32 nr_cpus = 1;
const volatile u64 slice_ns = 20000000;
const volatile u64 max_read_bandwidth_mb = 1000;
const volatile u64 max_write_bandwidth_mb = 1000;
const volatile bool enable_damon = true;
const volatile bool enable_cxl_aware = true;
const volatile bool enable_bandwidth_control = true;

enum task_type {
	TASK_TYPE_UNKNOWN = 0,
	TASK_TYPE_MOE_VECTORDB,
	TASK_TYPE_KWORKER,
	TASK_TYPE_REGULAR,
	TASK_TYPE_LATENCY_SENSITIVE,
	TASK_TYPE_READ_INTENSIVE,
	TASK_TYPE_WRITE_INTENSIVE,
	TASK_TYPE_BANDWIDTH_TEST,
};

enum io_pattern {
	IO_PATTERN_UNKNOWN = 0,
	IO_PATTERN_READ_HEAVY,
	IO_PATTERN_WRITE_HEAVY,
	IO_PATTERN_MIXED,
	IO_PATTERN_SEQUENTIAL,
	IO_PATTERN_RANDOM,
};

struct memory_access_pattern {
	u64 nr_accesses;
	u64 avg_access_size;
	u64 total_access_time;
	u64 last_access_time;
	u64 hot_regions;
	u64 cold_regions;
	u32 locality_score;
	u32 working_set_size;
	u64 read_bytes;
	u64 write_bytes;
	enum io_pattern io_pattern;
};

struct cxl_pmu_metrics {
	u64 memory_bandwidth;
	u64 cache_hit_rate;
	u64 memory_latency;
	u64 cxl_utilization;
	u64 read_bandwidth;
	u64 write_bandwidth;
	u64 last_update_time;
};

struct task_ctx {
	enum task_type type;
	struct memory_access_pattern mem_pattern;
	u32 priority_boost;
	u32 cpu_affinity_mask;
	u64 last_scheduled_time;
	u32 consecutive_migrations;
	bool is_memory_intensive;
	bool needs_promotion;
	bool is_bandwidth_critical;
	u32 preferred_dsq;
	u64 vtime;
	u64 token_bucket_tokens;
	u64 last_refill_time;
};

struct cpu_ctx {
	struct cxl_pmu_metrics cxl_metrics;
	u32 active_moe_tasks;
	u32 active_kworkers;
	u32 active_read_tasks;
	u32 active_write_tasks;
	u64 last_balance_time;
	bool is_cxl_attached;
	bool is_read_optimized;
	bool is_write_optimized;
	u64 available_read_bandwidth;
	u64 available_write_bandwidth;
	u64 last_bandwidth_update;
};

struct scheduler_stats {
	u64 total_enqueues;
	u64 total_dispatches;
	u64 vectordb_tasks;
	u64 bandwidth_limited_tasks;
	u64 damon_updates;
	u64 cxl_migrations;
} stats;

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, MAX_CPUS);
	__type(key, u32);
	__type(value, struct cpu_ctx);
} cpu_contexts SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, MAX_TASKS);
	__type(key, u32);
	__type(value, struct memory_access_pattern);
} damon_data SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 64);
	__type(key, u32);
	__type(value, u64);
} bandwidth_quota SEC(".maps");

static u64 global_vtime;

static inline bool vtime_before(u64 a, u64 b)
{
	return (s64)(a - b) < 0;
}

static bool is_moe_vectordb_task(struct task_struct *p)
{
	char comm[16];
	bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm);
	
	if (comm[0] == 'v' && comm[1] == 'e' && comm[2] == 'c' && comm[3] == 't')
		return true;
	if (comm[0] == 'f' && comm[1] == 'a' && comm[2] == 'i' && comm[3] == 's')
		return true;
	if (comm[0] == 'm' && comm[1] == 'i' && comm[2] == 'l' && comm[3] == 'v')
		return true;
	if (comm[0] == 'w' && comm[1] == 'e' && comm[2] == 'a' && comm[3] == 'v')
		return true;
	if (comm[0] == 'p' && comm[1] == 'y' && comm[2] == 't' && comm[3] == 'h')
		return true;
	
	return false;
}

static bool is_kworker_task(struct task_struct *p)
{
	char comm[16];
	bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm);
	return (comm[0] == 'k' && comm[1] == 'w' && comm[2] == 'o' && 
	        comm[3] == 'r' && comm[4] == 'k' && comm[5] == 'e' && comm[6] == 'r');
}

static bool is_bandwidth_test_task(struct task_struct *p)
{
	char comm[16];
	bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm);
	
	if (comm[0] == 'd' && comm[1] == 'o' && comm[2] == 'u' && comm[3] == 'b' && 
	    comm[4] == 'l' && comm[5] == 'e' && comm[6] == '_')
		return true;
	if (comm[0] == 'b' && comm[1] == 'a' && comm[2] == 'n' && comm[3] == 'd')
		return true;
	if (comm[0] == 'm' && comm[1] == 'e' && comm[2] == 'm' && comm[3] == 't')
		return true;
	if (comm[0] == 's' && comm[1] == 't' && comm[2] == 'r' && comm[3] == 'e')
		return true;
	
	return false;
}

static enum io_pattern classify_io_pattern(struct memory_access_pattern *pattern)
{
	if (!pattern || (pattern->read_bytes == 0 && pattern->write_bytes == 0))
		return IO_PATTERN_UNKNOWN;
		
	u64 total_bytes = pattern->read_bytes + pattern->write_bytes;
	u64 read_ratio = (pattern->read_bytes * 100) / total_bytes;
	
	if (read_ratio > 70)
		return IO_PATTERN_READ_HEAVY;
	else if (read_ratio < 30)
		return IO_PATTERN_WRITE_HEAVY;
	else
		return IO_PATTERN_MIXED;
}

static enum task_type classify_task(struct task_struct *p, struct memory_access_pattern *pattern)
{
	if (is_bandwidth_test_task(p))
		return TASK_TYPE_BANDWIDTH_TEST;
		
	if (is_moe_vectordb_task(p))
		return TASK_TYPE_MOE_VECTORDB;
		
	if (is_kworker_task(p))
		return TASK_TYPE_KWORKER;
	
	if (pattern) {
		enum io_pattern io_pat = classify_io_pattern(pattern);
		if (io_pat == IO_PATTERN_READ_HEAVY && 
		    pattern->read_bytes > MB_TO_BYTES(BANDWIDTH_THRESHOLD_MB))
			return TASK_TYPE_READ_INTENSIVE;
		if (io_pat == IO_PATTERN_WRITE_HEAVY && 
		    pattern->write_bytes > MB_TO_BYTES(BANDWIDTH_THRESHOLD_MB))
			return TASK_TYPE_WRITE_INTENSIVE;
	}
	
	return TASK_TYPE_REGULAR;
}

static u32 calculate_priority_boost(struct task_ctx *tctx)
{
	u32 boost = 0;
	
	switch (tctx->type) {
	case TASK_TYPE_BANDWIDTH_TEST:
		boost = 30;
		break;
	case TASK_TYPE_MOE_VECTORDB:
		boost = 25;
		break;
	case TASK_TYPE_LATENCY_SENSITIVE:
		boost = 25;
		break;
	case TASK_TYPE_READ_INTENSIVE:
		if (tctx->mem_pattern.read_bytes > MB_TO_BYTES(BANDWIDTH_THRESHOLD_MB))
			boost = 15;
		break;
	case TASK_TYPE_WRITE_INTENSIVE:
		if (tctx->mem_pattern.write_bytes > MB_TO_BYTES(BANDWIDTH_THRESHOLD_MB))
			boost = 15;
		break;
	case TASK_TYPE_KWORKER:
		if (tctx->needs_promotion)
			boost = 20;
		else
			boost = 5;
		break;
	default:
		boost = 0;
	}
	
	if (tctx->mem_pattern.locality_score > 80)
		boost += 5;
	
	return boost;
}

static bool refill_token_bucket(struct task_ctx *tctx, u64 now)
{
	if (!enable_bandwidth_control)
		return true;
		
	u64 elapsed = now - tctx->last_refill_time;
	if (elapsed < TOKEN_BUCKET_REFILL_INTERVAL_NS)
		return tctx->token_bucket_tokens > 0;
	
	u64 max_tokens;
	if (tctx->type == TASK_TYPE_READ_INTENSIVE)
		max_tokens = (max_read_bandwidth_mb * MB_TO_BYTES(1) * elapsed) / 1000000000;
	else if (tctx->type == TASK_TYPE_WRITE_INTENSIVE)
		max_tokens = (max_write_bandwidth_mb * MB_TO_BYTES(1) * elapsed) / 1000000000;
	else
		max_tokens = MB_TO_BYTES(100);
	
	tctx->token_bucket_tokens = max_tokens;
	tctx->last_refill_time = now;
	
	return true;
}

static s32 select_cpu_for_task(struct task_struct *p, struct task_ctx *tctx, s32 prev_cpu)
{
	u32 cpu;
	struct cpu_ctx *cctx;
	s32 best_cpu = prev_cpu;
	u64 best_score = 0;
	
	if (!enable_cxl_aware)
		return prev_cpu;
	
	bpf_for(cpu, 0, nr_cpus) {
		if (cpu >= nr_cpus)
			break;
			
		if (!bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			continue;
			
		cctx = bpf_map_lookup_elem(&cpu_contexts, &cpu);
		if (!cctx)
			continue;
		
		u64 score = 100;
		
		if (tctx->type == TASK_TYPE_MOE_VECTORDB && cctx->is_cxl_attached)
			score += 50;
		
		if (tctx->type == TASK_TYPE_READ_INTENSIVE && cctx->is_read_optimized)
			score += 30;
			
		if (tctx->type == TASK_TYPE_WRITE_INTENSIVE && cctx->is_write_optimized)
			score += 30;
		
		if (cctx->cxl_metrics.memory_latency < 100)
			score += 20;
			
		if (cctx->cxl_metrics.cache_hit_rate > 80)
			score += 15;
		
		score -= cctx->active_moe_tasks * 5;
		score -= cctx->active_read_tasks * 3;
		score -= cctx->active_write_tasks * 3;
		
		if (score > best_score) {
			best_score = score;
			best_cpu = cpu;
		}
	}
	
	if (best_cpu != prev_cpu && scx_bpf_test_and_clear_cpu_idle(best_cpu))
		return best_cpu;
		
	return prev_cpu;
}

s32 BPF_STRUCT_OPS(cxl_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	struct task_ctx *tctx;
	
	tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
	if (!tctx)
		return prev_cpu;
	
	return select_cpu_for_task(p, tctx, prev_cpu);
}

void BPF_STRUCT_OPS(cxl_enqueue, struct task_struct *p, u64 enq_flags)
{
	struct task_ctx *tctx;
	struct memory_access_pattern *damon_pattern;
	u32 pid = p->pid;
	u64 now = bpf_ktime_get_ns();
	u64 vtime = global_vtime;
	u64 slice = slice_ns;
	
	tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!tctx) {
		scx_bpf_dsq_insert(p, FALLBACK_DSQ_ID, slice, enq_flags);
		return;
	}
	
	if (enable_damon) {
		damon_pattern = bpf_map_lookup_elem(&damon_data, &pid);
		if (damon_pattern) {
			tctx->mem_pattern = *damon_pattern;
			__sync_fetch_and_add(&stats.damon_updates, 1);
		}
	}
	
	tctx->type = classify_task(p, &tctx->mem_pattern);
	
	if (tctx->type == TASK_TYPE_MOE_VECTORDB)
		__sync_fetch_and_add(&stats.vectordb_tasks, 1);
	
	tctx->priority_boost = calculate_priority_boost(tctx);
	
	if (!refill_token_bucket(tctx, now)) {
		__sync_fetch_and_add(&stats.bandwidth_limited_tasks, 1);
		scx_bpf_dsq_insert(p, FALLBACK_DSQ_ID, slice * 2, enq_flags);
		return;
	}
	
	if (vtime_before(vtime, tctx->vtime))
		vtime = tctx->vtime;
	else if (vtime_before(tctx->vtime, vtime - slice))
		vtime = vtime - slice;
	
	vtime -= (slice * tctx->priority_boost) / 100;
	
	tctx->vtime = vtime + slice;
	
	u32 dsq_id = FALLBACK_DSQ_ID;
	switch (tctx->type) {
	case TASK_TYPE_MOE_VECTORDB:
		dsq_id = VECTORDB_DSQ_ID;
		break;
	case TASK_TYPE_READ_INTENSIVE:
		dsq_id = READ_INTENSIVE_DSQ_ID;
		break;
	case TASK_TYPE_WRITE_INTENSIVE:
		dsq_id = WRITE_INTENSIVE_DSQ_ID;
		break;
	default:
		dsq_id = FALLBACK_DSQ_ID;
	}
	
	tctx->preferred_dsq = dsq_id;
	tctx->last_scheduled_time = now;
	
	__sync_fetch_and_add(&stats.total_enqueues, 1);
	
	scx_bpf_dsq_insert_vtime(p, dsq_id, slice, vtime, enq_flags);
}

void BPF_STRUCT_OPS(cxl_dispatch, s32 cpu, struct task_struct *prev)
{
	__sync_fetch_and_add(&stats.total_dispatches, 1);
	
	if (scx_bpf_dsq_move_to_local(VECTORDB_DSQ_ID, 0))
		return;
	if (scx_bpf_dsq_move_to_local(READ_INTENSIVE_DSQ_ID, 0))
		return;
	if (scx_bpf_dsq_move_to_local(WRITE_INTENSIVE_DSQ_ID, 0))
		return;
	
	scx_bpf_dsq_move_to_local(FALLBACK_DSQ_ID, 0);
}

void BPF_STRUCT_OPS(cxl_running, struct task_struct *p)
{
	struct task_ctx *tctx;
	struct cpu_ctx *cctx;
	u32 cpu = scx_bpf_task_cpu(p);
	
	tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
	if (!tctx)
		return;
	
	cctx = bpf_map_lookup_elem(&cpu_contexts, &cpu);
	if (!cctx)
		return;
	
	switch (tctx->type) {
	case TASK_TYPE_MOE_VECTORDB:
		cctx->active_moe_tasks++;
		break;
	case TASK_TYPE_READ_INTENSIVE:
		cctx->active_read_tasks++;
		break;
	case TASK_TYPE_WRITE_INTENSIVE:
		cctx->active_write_tasks++;
		break;
	case TASK_TYPE_KWORKER:
		cctx->active_kworkers++;
		break;
	default:
		break;
	}
	
	if (tctx->token_bucket_tokens > 0) {
		u64 consumed = slice_ns / 1000;
		if (tctx->token_bucket_tokens >= consumed)
			tctx->token_bucket_tokens -= consumed;
		else
			tctx->token_bucket_tokens = 0;
	}
}

void BPF_STRUCT_OPS(cxl_stopping, struct task_struct *p, bool runnable)
{
	struct task_ctx *tctx;
	struct cpu_ctx *cctx;
	u32 cpu = scx_bpf_task_cpu(p);
	
	tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, 0);
	if (!tctx)
		return;
	
	cctx = bpf_map_lookup_elem(&cpu_contexts, &cpu);
	if (!cctx)
		return;
	
	switch (tctx->type) {
	case TASK_TYPE_MOE_VECTORDB:
		if (cctx->active_moe_tasks > 0)
			cctx->active_moe_tasks--;
		break;
	case TASK_TYPE_READ_INTENSIVE:
		if (cctx->active_read_tasks > 0)
			cctx->active_read_tasks--;
		break;
	case TASK_TYPE_WRITE_INTENSIVE:
		if (cctx->active_write_tasks > 0)
			cctx->active_write_tasks--;
		break;
	case TASK_TYPE_KWORKER:
		if (cctx->active_kworkers > 0)
			cctx->active_kworkers--;
		break;
	default:
		break;
	}
	
	global_vtime = tctx->vtime;
}

void BPF_STRUCT_OPS(cxl_enable, struct task_struct *p)
{
	struct task_ctx *tctx;
	
	tctx = bpf_task_storage_get(&task_ctx_stor, p, 0, BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!tctx)
		return;
	
	tctx->vtime = global_vtime;
	tctx->last_scheduled_time = bpf_ktime_get_ns();
	tctx->token_bucket_tokens = MB_TO_BYTES(100);
	tctx->last_refill_time = bpf_ktime_get_ns();
}

s32 BPF_STRUCT_OPS_SLEEPABLE(cxl_init)
{
	u32 i;
	
	scx_bpf_create_dsq(FALLBACK_DSQ_ID, -1);
	scx_bpf_create_dsq(READ_INTENSIVE_DSQ_ID, -1);
	scx_bpf_create_dsq(WRITE_INTENSIVE_DSQ_ID, -1);
	scx_bpf_create_dsq(VECTORDB_DSQ_ID, -1);
	
	bpf_for(i, 0, nr_cpus) {
		struct cpu_ctx init_ctx = {
			.is_cxl_attached = (i < 4),
			.is_read_optimized = (i % 2 == 0),
			.is_write_optimized = (i % 2 == 1),
			.available_read_bandwidth = max_read_bandwidth_mb * MB_TO_BYTES(1),
			.available_write_bandwidth = max_write_bandwidth_mb * MB_TO_BYTES(1),
			.last_bandwidth_update = bpf_ktime_get_ns(),
		};
		u32 cpu_id = i;
		bpf_map_update_elem(&cpu_contexts, &cpu_id, &init_ctx, BPF_ANY);
	}
	
	return 0;
}

void BPF_STRUCT_OPS(cxl_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(cxl_ops,
	       .select_cpu		= (void *)cxl_select_cpu,
	       .enqueue			= (void *)cxl_enqueue,
	       .dispatch		= (void *)cxl_dispatch,
	       .running			= (void *)cxl_running,
	       .stopping		= (void *)cxl_stopping,
	       .enable			= (void *)cxl_enable,
	       .init			= (void *)cxl_init,
	       .exit			= (void *)cxl_exit,
	       .name			= "scx_cxl");
