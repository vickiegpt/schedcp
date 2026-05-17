/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_ddos_log_analysis - Priority scheduler for DDoS log analysis workload
 * 
 * Implementation: Two-queue scheduler with priority for spike detection
 * - Spike detection process (large_pandas_etl_test.py) gets highest priority
 * - Normal monitoring processes (small_pandas_etl_test.py) get standard priority
 * - Spike process gets longer time slices for sustained analysis
 */
#include <scx/common.bpf.h>
#include "pid_filename_header.h"

/* Detect if task is the spike analyzer */
static bool is_spike_analyzer(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for large_pandas_etl_test.py */
	int is_large = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 'l' && info->filename[3] == 'a' 
		&& info->filename[4] == 'r' && info->filename[5] == 'g' 
		&& info->filename[6] == 'e' && info->filename[7] == '_'
		&& info->filename[8] == 'p' && info->filename[9] == 'a'
		&& info->filename[10] == 'n' && info->filename[11] == 'd'
		&& info->filename[12] == 'a' && info->filename[13] == 's');
	
	return is_large;
}

/* Detect if task is a normal analyzer */
static bool is_normal_analyzer(struct task_struct *p)
{
	pid_t pid = p->pid;
	struct filename_info *info;
	
	/* Get filename info for this PID */
	info = bpf_map_lookup_elem(&pid_to_filename, &pid);
	if (!info)
		return false;
	
	/* Check for small_pandas_etl_test.py */
	int is_small = (info->filename[0] == '.' && info->filename[1] == '/' 
		&& info->filename[2] == 's' && info->filename[3] == 'm' 
		&& info->filename[4] == 'a' && info->filename[5] == 'l' 
		&& info->filename[6] == 'l' && info->filename[7] == '_'
		&& info->filename[8] == 'p' && info->filename[9] == 'a'
		&& info->filename[10] == 'n' && info->filename[11] == 'd'
		&& info->filename[12] == 'a' && info->filename[13] == 's');
	
	return is_small;
}

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

#define SPIKE_DSQ   0    /* High priority DSQ for spike analyzer */
#define NORMAL_DSQ  1    /* Normal priority DSQ for regular analyzers */

/* Time slice configurations */
#define SPIKE_SLICE_NS   20000000ULL   /* 20ms for spike analyzer */
#define NORMAL_SLICE_NS   5000000ULL   /* 5ms for normal analyzers */
#define DEFAULT_SLICE_NS  5000000ULL   /* 5ms for other tasks */

/* Get appropriate time slice based on task type */
static u64 get_time_slice(struct task_struct *p)
{
	if (is_spike_analyzer(p))
		return SPIKE_SLICE_NS;
	else if (is_normal_analyzer(p))
		return NORMAL_SLICE_NS;
	else
		return DEFAULT_SLICE_NS;
}

s32 BPF_STRUCT_OPS(ddos_log_analysis_select_cpu, struct task_struct *p, s32 prev_cpu, u64 wake_flags)
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

void BPF_STRUCT_OPS(ddos_log_analysis_enqueue, struct task_struct *p, u64 enq_flags)
{
	u64 slice_ns = get_time_slice(p);
	
	/* Priority enqueue: spike analyzer goes to high priority queue */
	if (is_spike_analyzer(p)) {
		scx_bpf_dsq_insert(p, SPIKE_DSQ, slice_ns, enq_flags);
	} else if (is_normal_analyzer(p)) {
		scx_bpf_dsq_insert(p, NORMAL_DSQ, slice_ns, enq_flags);
	} else {
		/* Other tasks go to normal queue */
		scx_bpf_dsq_insert(p, NORMAL_DSQ, slice_ns, enq_flags);
	}
}

void BPF_STRUCT_OPS(ddos_log_analysis_dispatch, s32 cpu, struct task_struct *prev)
{
	/* Priority dispatch: always try spike analyzer first */
	if (!scx_bpf_dsq_move_to_local(SPIKE_DSQ, 0)) {
		/* If no spike analyzer, dispatch normal tasks */
		scx_bpf_dsq_move_to_local(NORMAL_DSQ, 0);
	}
}

s32 BPF_STRUCT_OPS_SLEEPABLE(ddos_log_analysis_init)
{
	s32 ret;
	
	/* Create DSQ for spike analyzer */
	ret = scx_bpf_create_dsq(SPIKE_DSQ, -1);
	if (ret)
		return ret;
	
	/* Create DSQ for normal analyzers */
	return scx_bpf_create_dsq(NORMAL_DSQ, -1);
}

void BPF_STRUCT_OPS(ddos_log_analysis_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(ddos_log_analysis_ops,
	       .select_cpu		= (void *)ddos_log_analysis_select_cpu,
	       .enqueue			= (void *)ddos_log_analysis_enqueue,
	       .dispatch		= (void *)ddos_log_analysis_dispatch,
	       .init			= (void *)ddos_log_analysis_init,
	       .exit			= (void *)ddos_log_analysis_exit,
	       .name			= "ddos_log_analysis");
