/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_hotkey_aggregation - Scheduler optimized for hot key aggregation workload
 * 
 * Implementation: Priority scheduling for skewed data processing
 * - Uses two separate DSQs: one for large skewed task, one for small tasks
 * - Always dispatches from large task queue first (highest priority)
 * - Falls back to small task queue when no large tasks available
 * - Optimized time slices for memory-intensive aggregation
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is a large skewed task by checking filename */
static bool is_large_skewed_task(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for large_spark_skew_test.py */
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 'l' && info->filename[3] == 'a' 
		&& info->filename[4] == 'r' && info->filename[5] == 'g' 
		&& info->filename[6] == 'e' && info->filename[7] == '_'
		&& info->filename[8] == 's' && info->filename[9] == 'p'
		&& info->filename[10] == 'a' && info->filename[11] == 'r'
		&& info->filename[12] == 'k');

	return is_large;
}

/* Check if task is a small skewed task */
static bool is_small_skewed_task(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for small_spark_skew_test.py */
	int is_small = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 's' && info->filename[3] == 'm' 
		&& info->filename[4] == 'a' && info->filename[5] == 'l' 
		&& info->filename[6] == 'l' && info->filename[7] == '_'
		&& info->filename[8] == 's' && info->filename[9] == 'p'
		&& info->filename[10] == 'a' && info->filename[11] == 'r'
		&& info->filename[12] == 'k');

	return is_small;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_DSQ  0    /* Highest priority DSQ for large skewed task */
#define SMALL_DSQ  1    /* Lower priority DSQ for small skewed tasks */
#define OTHER_DSQ  2    /* Lowest priority DSQ for other tasks */

/* Time slice configurations optimized for memory-intensive aggregation */
#define LARGE_SLICE_NS  20000000ULL   /* 20ms for large skewed task */
#define SMALL_SLICE_NS   5000000ULL   /* 5ms for small skewed tasks */
#define OTHER_SLICE_NS   2000000ULL   /* 2ms for other tasks */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	if (is_large_skewed_task(p))
		return LARGE_SLICE_NS;
	else if (is_small_skewed_task(p))
		return SMALL_SLICE_NS;
	else
		return OTHER_SLICE_NS;
}

s32 BPF_STRUCT_OPS(hotkey_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(hotkey_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue based on task type */
	if (is_large_skewed_task(p)) {
		scx_bpf_dsq_insert(p, LARGE_DSQ, slice_ns, enq_flags);
	} else if (is_small_skewed_task(p)) {
		scx_bpf_dsq_insert(p, SMALL_DSQ, slice_ns, enq_flags);
	} else {
		scx_bpf_dsq_insert(p, OTHER_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(hotkey_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: try queues in order of priority */
	if (!scx_bpf_dsq_move_to_local(LARGE_DSQ, 0)) {
		if (!scx_bpf_dsq_move_to_local(SMALL_DSQ, 0)) {
			scx_bpf_dsq_move_to_local(OTHER_DSQ, 0);
		}
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(hotkey_init)
{
	s32 ret;
	
	/* Create DSQ for large skewed task */
	ret = scx_bpf_create_dsq(LARGE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small skewed tasks */
	ret = scx_bpf_create_dsq(SMALL_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for other tasks */
	return scx_bpf_create_dsq(OTHER_DSQ, -1);
}

void BPF_STRUCT_OPS(hotkey_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(hotkey_ops,
	       .select_cpu		= (void *)hotkey_select_cpu,
	       .enqueue			= (void *)hotkey_enqueue,
	       .dispatch		= (void *)hotkey_dispatch,
	       .init			= (void *)hotkey_init,
	       .exit			= (void *)hotkey_exit,
	       .name			= "hotkey_aggregation");
