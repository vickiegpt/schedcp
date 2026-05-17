/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_video_transcode - Priority scheduler for video transcoding workloads
 * 
 * Implementation: Prioritizes HD video processing over smaller videos
 * - Uses two separate DSQs: one for large (HD) video tasks, one for small video tasks  
 * - Always dispatches from large video queue first (priority)
 * - Falls back to small video queue when no large tasks available
 * - Optimized time slices for video encoding operations
 */
#include <scx/common.bpf.h>

/* Detect if task is a large (HD) video transcode task */
static bool is_large_video_task(struct task_struct *p)
{
	char comm[16];
	
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) < 0)
		return false;
	
	/* Check for "large_video_transcode" process */
	return (comm[0] == 'l' && comm[1] == 'a' && comm[2] == 'r' && 
	        comm[3] == 'g' && comm[4] == 'e' && comm[5] == '_' &&
	        comm[6] == 'v' && comm[7] == 'i' && comm[8] == 'd' &&
	        comm[9] == 'e' && comm[10] == 'o');
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LARGE_VIDEO_DSQ  0    /* High priority DSQ for HD video tasks */
#define SMALL_VIDEO_DSQ  1    /* Lower priority DSQ for small video tasks */

/* Time slice configurations optimized for video encoding */
#define LARGE_VIDEO_SLICE_NS  30000000ULL   /* 30ms for HD video encoding */
#define SMALL_VIDEO_SLICE_NS  10000000ULL   /* 10ms for small video encoding */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	return is_large_video_task(p) ? LARGE_VIDEO_SLICE_NS : SMALL_VIDEO_SLICE_NS;
}

s32 BPF_STRUCT_OPS(video_transcode_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(video_transcode_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: large video tasks go to high priority queue */
	if (is_large_video_task(p)) {
		scx_bpf_dsq_insert(p, LARGE_VIDEO_DSQ, slice_ns, enq_flags);
	} else {
		scx_bpf_dsq_insert(p, SMALL_VIDEO_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(video_transcode_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try large video tasks first */
	if (!scx_bpf_dsq_move_to_local(LARGE_VIDEO_DSQ, 0)) {
		/* If no large video tasks, dispatch small video tasks */
		scx_bpf_dsq_move_to_local(SMALL_VIDEO_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(video_transcode_init)
{
	s32 ret;
	
	/* Create DSQ for large video tasks */
	ret = scx_bpf_create_dsq(LARGE_VIDEO_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for small video tasks */
	return scx_bpf_create_dsq(SMALL_VIDEO_DSQ, -1);
}

void BPF_STRUCT_OPS(video_transcode_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(video_transcode_ops,
	       .select_cpu		= (void *)video_transcode_select_cpu,
	       .enqueue			= (void *)video_transcode_enqueue,
	       .dispatch		= (void *)video_transcode_dispatch,
	       .init			= (void *)video_transcode_init,
	       .exit			= (void *)video_transcode_exit,
	       .name			= "video_transcode");
