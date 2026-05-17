/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_ctest - FIFO scheduler with long task priority
 * 
 * Implementation: FIFO scheduling with priority for long tasks
 * - Uses two separate DSQs: one for long tasks, one for short tasks
 * - Always dispatches from long task queue first (priority FIFO)
 * - Falls back to short task queue when no long tasks available
 * - Now uses filename-based filtering instead of comm
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is a "long" task by checking filename */
static bool is_long_task(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	char local_filename[MAX_FILENAME_LEN];
	const char *basename;
	int last_slash = -1;

	char comm[16];
	
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) < 0)
		return false;
	
	/* Check for start with "large" */
	// for C program, you should use the comm name to check the application, not the filename.
	int is_comm_large = (comm[0] == 'l' && comm[1] == 'a' && comm[2] == 'r' && 
	        comm[3] == 'g' && comm[4] == 'e');
	if (is_comm_large) {
		// bpf_printk("large task: %s\n", comm);
		return true;
	}
	// for C program, you should use the comm name to check the application, not the filename.
	int is_comm_long = (comm[0] == 'l' && comm[1] == 'o' && comm[2] == 'n' && 
	        comm[3] == 'g');
	if (is_comm_long) {
		// bpf_printk("long task: %s\n", comm);
		return true;
	}
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	// for python program, you should use the filename to check the application, not the comm name.
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 'l' && info->filename[3] == 'a' 
		&& info->filename[4] == 'r' && info->filename[5] == 'g' 
		&& info->filename[6] == 'e');

	if (is_large) {
		// bpf_printk("large task: %s\n", info->filename);
		return true;
	}

	return false;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LONG_DSQ  0    /* High priority DSQ for long tasks */
#define SHORT_DSQ 1    /* Lower priority DSQ for short tasks */

/* Time slice configurations */
#define SHORT_SLICE_NS  30000000ULL   /* 3ms for short tasks */
#define LONG_SLICE_NS   15000000ULL  /* 15ms for long tasks */


/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	return is_long_task(p) ? LONG_SLICE_NS : SHORT_SLICE_NS;
}

s32 BPF_STRUCT_OPS(ctest_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(ctest_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* FIFO enqueue: long tasks go to high priority queue */
	if (is_long_task(p)) {
		scx_bpf_dsq_insert(p, LONG_DSQ, slice_ns, enq_flags);
	} else {
		scx_bpf_dsq_insert(p, SHORT_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(ctest_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try long tasks first */
	if (!scx_bpf_dsq_move_to_local(LONG_DSQ, 0)) {
		/* If no long tasks, dispatch short tasks */
		scx_bpf_dsq_move_to_local(SHORT_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(ctest_init)
{
	s32 ret;
	
	/* Create DSQ for long tasks */
	ret = scx_bpf_create_dsq(LONG_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for short tasks */
	return scx_bpf_create_dsq(SHORT_DSQ, -1);
}

void BPF_STRUCT_OPS(ctest_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(ctest_ops,
	       .select_cpu		= (void *)ctest_select_cpu,
	       .enqueue			= (void *)ctest_enqueue,
	       .dispatch		= (void *)ctest_dispatch,
	       .init			= (void *)ctest_init,
	       .exit			= (void *)ctest_exit,
	       .name			= "ctest");
