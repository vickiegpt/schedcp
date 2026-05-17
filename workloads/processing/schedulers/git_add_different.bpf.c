/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_git_add_different - Priority scheduler for git add operations
 * 
 * Implementation: Prioritizes large git add operations over small ones
 * - Uses two separate DSQs: one for large git operations, one for small
 * - Always dispatches from large git queue first (priority)
 * - Falls back to small git queue when no large operations available
 */
#include <scx/common.bpf.h>

/* Detect if task is a large git add operation */
static bool is_large_git_add(struct task_struct *p)
{
	char comm[16];
	
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) < 0)
		return false;
	
	/* Check for "large_git_add" command name */
	return (comm[0] == 'l' && comm[1] == 'a' && comm[2] == 'r' && 
	        comm[3] == 'g' && comm[4] == 'e' && comm[5] == '_' &&
	        comm[6] == 'g' && comm[7] == 'i' && comm[8] == 't' &&
	        comm[9] == '_' && comm[10] == 'a' && comm[11] == 'd' &&
	        comm[12] == 'd');
}

/* Detect if task is a small git add operation */
static bool is_small_git_add(struct task_struct *p)
{
	char comm[16];
	
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) < 0)
		return false;
	
	/* Check for "small_git_add" command name */
	return (comm[0] == 's' && comm[1] == 'm' && comm[2] == 'a' && 
	        comm[3] == 'l' && comm[4] == 'l' && comm[5] == '_' &&
	        comm[6] == 'g' && comm[7] == 'i' && comm[8] == 't' &&
	        comm[9] == '_' && comm[10] == 'a' && comm[11] == 'd' &&
	        comm[12] == 'd');
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_DSQ  0    /* High priority DSQ for large git operations */
#define SMALL_DSQ  1    /* Lower priority DSQ for small git operations */

/* Time slice configurations as per optimization goals */
#define LARGE_SLICE_NS  25000000ULL   /* 25ms for large git operations */
#define SMALL_SLICE_NS   5000000ULL   /* 5ms for small git operations */
#define DEFAULT_SLICE_NS 10000000ULL  /* 10ms for other tasks */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	if (is_large_git_add(p))
		return LARGE_SLICE_NS;
	else if (is_small_git_add(p))
		return SMALL_SLICE_NS;
	else
		return DEFAULT_SLICE_NS;
}

s32 BPF_STRUCT_OPS(git_add_different_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(git_add_different_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: large git operations go to high priority queue */
	if (is_large_git_add(p)) {
		scx_bpf_dsq_insert(p, LARGE_DSQ, slice_ns, enq_flags);
	} else if (is_small_git_add(p)) {
		scx_bpf_dsq_insert(p, SMALL_DSQ, slice_ns, enq_flags);
	} else {
		/* Other tasks go to global DSQ */
		scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(git_add_different_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try large git operations first */
	if (!scx_bpf_dsq_move_to_local(LARGE_DSQ, 0)) {
		/* If no large operations, dispatch small git operations */
		if (!scx_bpf_dsq_move_to_local(SMALL_DSQ, 0)) {
			/* Finally, dispatch other tasks from global queue */
			scx_bpf_dsq_move_to_local(SCX_DSQ_GLOBAL, 0);
		}
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(git_add_different_init)
{
	s32 ret;
	
	/* Create DSQ for large git operations */
	ret = scx_bpf_create_dsq(LARGE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small git operations */
	return scx_bpf_create_dsq(SMALL_DSQ, -1);
}

void BPF_STRUCT_OPS(git_add_different_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(git_add_different_ops,
	       .select_cpu		= (void *)git_add_different_select_cpu,
	       .enqueue			= (void *)git_add_different_enqueue,
	       .dispatch		= (void *)git_add_different_dispatch,
	       .init			= (void *)git_add_different_init,
	       .exit			= (void *)git_add_different_exit,
	       .name			= "git_add_different");
