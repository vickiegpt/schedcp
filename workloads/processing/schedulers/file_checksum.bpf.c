/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_file_checksum - Priority scheduler for file checksum workload
 * 
 * Implementation: Prioritizes large_file_checksum.py over small_file_checksum.py
 * - Uses two separate DSQs: one for large file process, one for small file processes
 * - Always dispatches from large file queue first (priority)
 * - Falls back to small file queue when no large file process available
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is the large file checksum process */
static bool is_large_file_checksum(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check if filename contains "large_file_checksum.py" */
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 'l' && info->filename[3] == 'a' 
		&& info->filename[4] == 'r' && info->filename[5] == 'g' 
		&& info->filename[6] == 'e' && info->filename[7] == '_'
		&& info->filename[8] == 'f' && info->filename[9] == 'i'
		&& info->filename[10] == 'l' && info->filename[11] == 'e'
		&& info->filename[12] == '_' && info->filename[13] == 'c'
		&& info->filename[14] == 'h' && info->filename[15] == 'e'
		&& info->filename[16] == 'c' && info->filename[17] == 'k');

	return is_large;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_DSQ  0    /* High priority DSQ for large file checksum */
#define SMALL_DSQ  1    /* Lower priority DSQ for small file checksums */

/* Time slice configurations */
#define LARGE_SLICE_NS   20000000ULL  /* 20ms for large file process */
#define SMALL_SLICE_NS    5000000ULL  /* 5ms for small file processes */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	return is_large_file_checksum(p) ? LARGE_SLICE_NS : SMALL_SLICE_NS;
}

s32 BPF_STRUCT_OPS(file_checksum_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(file_checksum_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: large file process goes to high priority queue */
	if (is_large_file_checksum(p)) {
		scx_bpf_dsq_insert(p, LARGE_DSQ, slice_ns, enq_flags);
	} else {
		scx_bpf_dsq_insert(p, SMALL_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(file_checksum_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try large file process first */
	if (!scx_bpf_dsq_move_to_local(LARGE_DSQ, 0)) {
		/* If no large file process, dispatch small file processes */
		scx_bpf_dsq_move_to_local(SMALL_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(file_checksum_init)
{
	s32 ret;
	
	/* Create DSQ for large file process */
	ret = scx_bpf_create_dsq(LARGE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small file processes */
	return scx_bpf_create_dsq(SMALL_DSQ, -1);
}

void BPF_STRUCT_OPS(file_checksum_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(file_checksum_ops,
	       .select_cpu		= (void *)file_checksum_select_cpu,
	       .enqueue			= (void *)file_checksum_enqueue,
	       .dispatch		= (void *)file_checksum_dispatch,
	       .init			= (void *)file_checksum_init,
	       .exit			= (void *)file_checksum_exit,
	       .name			= "file_checksum");
