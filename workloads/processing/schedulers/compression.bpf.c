/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_compression - Priority scheduler for compression workload
 * 
 * Implementation: Priority scheduling for compression tasks
 * - Uses two separate DSQs: one for large compression, one for small compression
 * - Always dispatches from large compression queue first (priority)
 * - Falls back to small compression queue when no large tasks available
 * - Large compression gets longer time slices for throughput
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is large compression by checking filename */
static bool is_large_compression(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for ./large_compression.py */
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' &&
			info->filename[2] == 'l' && info->filename[3] == 'a' &&
			info->filename[4] == 'r' && info->filename[5] == 'g' &&
			info->filename[6] == 'e' && info->filename[7] == '_' &&
			info->filename[8] == 'c' && info->filename[9] == 'o' &&
			info->filename[10] == 'm' && info->filename[11] == 'p');
	
	return is_large;
}

/* Detect if task is small compression by checking filename */
static bool is_small_compression(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for ./small_compression.py */
	int is_small = (info->filename[0] == '.' && info->filename[1] == '/' &&
			info->filename[2] == 's' && info->filename[3] == 'm' &&
			info->filename[4] == 'a' && info->filename[5] == 'l' &&
			info->filename[6] == 'l' && info->filename[7] == '_' &&
			info->filename[8] == 'c' && info->filename[9] == 'o' &&
			info->filename[10] == 'm' && info->filename[11] == 'p');
	
	return is_small;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_DSQ  0    /* High priority DSQ for large compression */
#define SMALL_DSQ 1    /* Lower priority DSQ for small compression */

/* Time slice configurations */
#define SMALL_SLICE_NS  3000000ULL   /* 3ms for small compression tasks */
#define LARGE_SLICE_NS  15000000ULL  /* 15ms for large compression task */
#define DEFAULT_SLICE_NS 5000000ULL  /* 5ms for other tasks */

s32 BPF_STRUCT_OPS(compression_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu;
	u64 slice_ns = DEFAULT_SLICE_NS;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		/* Determine time slice based on task type */
		if (is_large_compression(p))
			slice_ns = LARGE_SLICE_NS;
		else if (is_small_compression(p))
			slice_ns = SMALL_SLICE_NS;
		
		/* For idle CPU, dispatch directly to local DSQ */
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_ns, 0);
	}

	return cpu;
}

void BPF_STRUCT_OPS(compression_enqueue, struct task_struct *p, u64 enq_flags)
{
	/* Enqueue based on task type */
	if (is_large_compression(p)) {
		scx_bpf_dsq_insert(p, LARGE_DSQ, LARGE_SLICE_NS, enq_flags);
	} else if (is_small_compression(p)) {
		scx_bpf_dsq_insert(p, SMALL_DSQ, SMALL_SLICE_NS, enq_flags);
	} else {
		/* Other tasks go to global DSQ */
		scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, DEFAULT_SLICE_NS, enq_flags);
	}
}

void BPF_STRUCT_OPS(compression_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try large compression first */
	if (!scx_bpf_dsq_move_to_local(LARGE_DSQ, 0)) {
		/* If no large tasks, dispatch small compression tasks */
		if (!scx_bpf_dsq_move_to_local(SMALL_DSQ, 0)) {
			/* Finally, dispatch other tasks */
			scx_bpf_dsq_move_to_local(SCX_DSQ_GLOBAL, 0);
		}
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(compression_init)
{
	s32 ret;
	
	/* Create DSQ for large compression */
	ret = scx_bpf_create_dsq(LARGE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small compression */
	return scx_bpf_create_dsq(SMALL_DSQ, -1);
}

void BPF_STRUCT_OPS(compression_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(compression_ops,
	       .select_cpu		= (void *)compression_select_cpu,
	       .enqueue			= (void *)compression_enqueue,
	       .dispatch		= (void *)compression_dispatch,
	       .init			= (void *)compression_init,
	       .exit			= (void *)compression_exit,
	       .name			= "compression");
