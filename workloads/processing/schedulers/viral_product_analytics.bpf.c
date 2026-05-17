/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_viral_product_analytics - Scheduler optimized for viral product analytics workload
 * 
 * Implementation: Priority scheduling for large analytics processing
 * - Uses two separate DSQs: one for large analytics (viral surge), one for small analytics
 * - Always dispatches from large analytics queue first (priority)
 * - Falls back to small analytics queue when no large analytics available
 * - Optimized time slices for join-heavy operations
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is a large analytics task by checking filename */
static bool is_large_analytics_task(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for "./large_flink_join_test.py" */
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 'l' && info->filename[3] == 'a' 
		&& info->filename[4] == 'r' && info->filename[5] == 'g' 
		&& info->filename[6] == 'e' && info->filename[7] == '_'
		&& info->filename[8] == 'f' && info->filename[9] == 'l'
		&& info->filename[10] == 'i' && info->filename[11] == 'n'
		&& info->filename[12] == 'k');

	return is_large;
}

/* Detect if task is a small analytics task by checking filename */
static bool is_small_analytics_task(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for "./small_flink_join_test.py" */
	int is_small = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 's' && info->filename[3] == 'm' 
		&& info->filename[4] == 'a' && info->filename[5] == 'l' 
		&& info->filename[6] == 'l' && info->filename[7] == '_'
		&& info->filename[8] == 'f' && info->filename[9] == 'l'
		&& info->filename[10] == 'i' && info->filename[11] == 'n'
		&& info->filename[12] == 'k');

	return is_small;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_DSQ  0    /* High priority DSQ for large analytics (viral surge) */
#define SMALL_DSQ  1    /* Lower priority DSQ for small analytics */

/* Time slice configurations optimized for join-heavy operations */
#define LARGE_SLICE_NS  25000000ULL   /* 25ms for large analytics complex joins */
#define SMALL_SLICE_NS   5000000ULL   /* 5ms for small analytics regular processing */
#define DEFAULT_SLICE_NS 3000000ULL   /* 3ms for other tasks */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	if (is_large_analytics_task(p))
		return LARGE_SLICE_NS;
	else if (is_small_analytics_task(p))
		return SMALL_SLICE_NS;
	else
		return DEFAULT_SLICE_NS;
}

s32 BPF_STRUCT_OPS(viral_product_analytics_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		/* For idle CPU, dispatch directly to local DSQ with appropriate slice */
		u64 slice_ns = get_time_slice(p);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_ns, 0);
	}

	return cpu;
}

void BPF_STRUCT_OPS(viral_product_analytics_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: large analytics go to high priority queue */
	if (is_large_analytics_task(p)) {
		scx_bpf_dsq_insert(p, LARGE_DSQ, slice_ns, enq_flags);
	} else if (is_small_analytics_task(p)) {
		scx_bpf_dsq_insert(p, SMALL_DSQ, slice_ns, enq_flags);
	} else {
		/* Other tasks go to small queue with lower priority */
		scx_bpf_dsq_insert(p, SMALL_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(viral_product_analytics_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try large analytics first */
	if (!scx_bpf_dsq_move_to_local(LARGE_DSQ, 0)) {
		/* If no large analytics, dispatch small analytics and other tasks */
		scx_bpf_dsq_move_to_local(SMALL_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(viral_product_analytics_init)
{
	s32 ret;
	
	/* Create DSQ for large analytics */
	ret = scx_bpf_create_dsq(LARGE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small analytics */
	return scx_bpf_create_dsq(SMALL_DSQ, -1);
}

void BPF_STRUCT_OPS(viral_product_analytics_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(viral_product_analytics_ops,
	       .select_cpu		= (void *)viral_product_analytics_select_cpu,
	       .enqueue			= (void *)viral_product_analytics_enqueue,
	       .dispatch		= (void *)viral_product_analytics_dispatch,
	       .init			= (void *)viral_product_analytics_init,
	       .exit			= (void *)viral_product_analytics_exit,
	       .name			= "viral_product_analytics");
