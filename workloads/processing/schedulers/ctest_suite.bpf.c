/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_ctest_suite - Priority scheduler for CTest suite workload
 * 
 * Implementation: Priority scheduling for test suite with fast unit tests and slow integration test
 * - Uses two separate DSQs: high priority for "long" integration test, normal for "short" unit tests
 * - Always dispatches from long task queue first
 * - Falls back to short task queue when no long tasks available
 * - Uses comm-based filtering for C programs
 */
#include <scx/common.bpf.h>

/* Detect if task is a "long" integration test by checking comm name */
static bool is_long_test(struct task_struct *p)
{
	char comm[16];
	
	if (bpf_probe_read_kernel_str(comm, sizeof(comm), p->comm) < 0)
		return false;
	
	/* Check for "long" test process */
	return (comm[0] == 'l' && comm[1] == 'o' && comm[2] == 'n' && comm[3] == 'g');
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define LONG_DSQ  0    /* High priority DSQ for long integration test */
#define SHORT_DSQ 1    /* Normal priority DSQ for short unit tests */

/* Time slice configurations as specified in optimization goals */
#define SHORT_SLICE_NS  3000000ULL   /* 3ms for short unit tests */
#define LONG_SLICE_NS   15000000ULL  /* 15ms for long integration test */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	return is_long_test(p) ? LONG_SLICE_NS : SHORT_SLICE_NS;
}

s32 BPF_STRUCT_OPS(ctest_suite_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(ctest_suite_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: long test goes to high priority queue */
	if (is_long_test(p)) {
		scx_bpf_dsq_insert(p, LONG_DSQ, slice_ns, enq_flags);
	} else {
		scx_bpf_dsq_insert(p, SHORT_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(ctest_suite_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try long test queue first */
	if (!scx_bpf_dsq_move_to_local(LONG_DSQ, 0)) {
		/* If no long test, dispatch short tests */
		scx_bpf_dsq_move_to_local(SHORT_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(ctest_suite_init)
{
	s32 ret;
	
	/* Create DSQ for long integration test */
	ret = scx_bpf_create_dsq(LONG_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for short unit tests */
	return scx_bpf_create_dsq(SHORT_DSQ, -1);
}

void BPF_STRUCT_OPS(ctest_suite_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(ctest_suite_ops,
	       .select_cpu		= (void *)ctest_suite_select_cpu,
	       .enqueue			= (void *)ctest_suite_enqueue,
	       .dispatch		= (void *)ctest_suite_dispatch,
	       .init			= (void *)ctest_suite_init,
	       .exit			= (void *)ctest_suite_exit,
	       .name			= "ctest_suite");
