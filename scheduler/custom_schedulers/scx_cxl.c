/* SPDX-License-Identifier: GPL-2.0 */
/*
 * CXL Bandwidth-Aware Scheduler Controller
 * 
 * This program loads and controls the CXL PMU-aware eBPF scheduler
 * with enhanced bandwidth optimization for read/write intensive workloads.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
#include <getopt.h>
#include <pthread.h>
#include <sys/sysinfo.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <fcntl.h>
#include <scx/common.h>
#include "scx_cxl.h"
#include "scx_cxl.bpf.skel.h"

#define MAX_CPUS 1024
#define MAX_TASKS 8192
#define DAMON_PATH "/sys/kernel/mm/damon/admin"
#define STATS_INTERVAL_SEC 1

static const char help_fmt[] =
"A CXL PMU-aware scheduler with DAMON integration for MoE VectorDB workloads.\n"
"\n"
"This scheduler optimizes for memory bandwidth-intensive workloads using CXL\n"
"PMU metrics and DAMON memory access patterns. It provides intelligent scheduling\n"
"for MoE VectorDB, read/write intensive tasks, and implements bandwidth control.\n"
"\n"
"Usage: %s [-h] [-v] [-s SLICE_US] [-n NR_CPUS] [-r READ_BW] [-w WRITE_BW]\n"
"          [-d] [-c] [-b] [-m MONITOR_SEC] [-D DAMON_PATH]\n"
"\n"
"  -h, --help            Show this help message\n"
"  -v, --verbose         Enable verbose output\n"
"  -s, --slice-us        Time slice in microseconds (default: 20000)\n"
"  -n, --nr-cpus         Number of CPUs to use (default: all)\n"
"  -r, --read-bw         Max read bandwidth in MB/s (default: 1000)\n"
"  -w, --write-bw        Max write bandwidth in MB/s (default: 1000)\n"
"  -d, --disable-damon   Disable DAMON integration\n"
"  -c, --disable-cxl     Disable CXL-aware CPU selection\n"
"  -b, --disable-bw-ctrl Disable bandwidth control\n"
"  -m, --monitor         Monitor interval in seconds (default: 1)\n"
"  -D, --damon-path      Path to DAMON sysfs (default: /sys/kernel/mm/damon/admin)\n"
"\n"
"Examples:\n"
"  # Run with default settings\n"
"  sudo %s\n"
"\n"
"  # Run with custom bandwidth limits\n"
"  sudo %s -r 800 -w 600\n"
"\n"
"  # Run with verbose output and monitoring\n"
"  sudo %s -v -m 2\n"
"\n"
"  # Run with disabled DAMON and bandwidth control\n"
"  sudo %s -d -b\n"
"\n";

struct scheduler_config {
	bool verbose;
	u64 slice_us;
	u32 nr_cpus;
	u64 max_read_bandwidth_mb;
	u64 max_write_bandwidth_mb;
	bool enable_damon;
	bool enable_cxl_aware;
	bool enable_bandwidth_control;
	u32 monitor_interval_sec;
	const char *damon_path;
};

static volatile sig_atomic_t exit_req = 0;
static struct scx_cxl *skel;
static struct scheduler_config config = {
	.verbose = false,
	.slice_us = 20000,
	.nr_cpus = 0,
	.max_read_bandwidth_mb = 1000,
	.max_write_bandwidth_mb = 1000,
	.enable_damon = true,
	.enable_cxl_aware = true,
	.enable_bandwidth_control = true,
	.monitor_interval_sec = 1,
	.damon_path = DAMON_PATH,
};

static void sigint_handler(int sig)
{
	exit_req = 1;
}

static int setup_damon(const char *damon_path)
{
	char path[256];
	int fd;
	
	if (!config.enable_damon) {
		if (config.verbose)
			printf("DAMON integration disabled\n");
		return 0;
	}
	
	snprintf(path, sizeof(path), "%s/kdamonds/nr_kdamonds", damon_path);
	fd = open(path, O_WRONLY);
	if (fd < 0) {
		if (errno == ENOENT) {
			fprintf(stderr, "DAMON not available (kernel not configured with CONFIG_DAMON_SYSFS)\n");
			config.enable_damon = false;
			return 0;
		}
		perror("Failed to open DAMON sysfs");
		return -1;
	}
	
	if (write(fd, "1\n", 2) != 2) {
		perror("Failed to create DAMON context");
		close(fd);
		return -1;
	}
	close(fd);
	
	snprintf(path, sizeof(path), "%s/kdamonds/0/contexts/nr_contexts", damon_path);
	fd = open(path, O_WRONLY);
	if (fd >= 0) {
		if (write(fd, "1\n", 2) < 0) {}
		// Ignore return value
		close(fd);
	}
	
	snprintf(path, sizeof(path), "%s/kdamonds/0/state", damon_path);
	fd = open(path, O_WRONLY);
	if (fd >= 0) {
		if (write(fd, "on\n", 3) < 0) {}
		// Ignore return value
		close(fd);
	}
	
	if (config.verbose)
		printf("DAMON monitoring enabled at %s\n", damon_path);
	
	return 0;
}

static void cleanup_damon(const char *damon_path)
{
	char path[256];
	int fd;
	
	if (!config.enable_damon)
		return;
	
	snprintf(path, sizeof(path), "%s/kdamonds/0/state", damon_path);
	fd = open(path, O_WRONLY);
	if (fd >= 0) {
		if (write(fd, "off\n", 4) < 0) {}
		// Ignore return value
		close(fd);
	}
	
	if (config.verbose)
		printf("DAMON monitoring disabled\n");
}

static void print_stats(struct scheduler_stats *stats)
{
	static struct scheduler_stats prev_stats = {0};
	u64 delta_enqueues = stats->total_enqueues - prev_stats.total_enqueues;
	u64 delta_dispatches = stats->total_dispatches - prev_stats.total_dispatches;
	
	printf("\n=== CXL Scheduler Statistics ===\n");
	printf("Total enqueues:          %lu (delta: %lu)\n", stats->total_enqueues, delta_enqueues);
	printf("Total dispatches:        %lu (delta: %lu)\n", stats->total_dispatches, delta_dispatches);
	printf("VectorDB tasks:          %lu\n", stats->vectordb_tasks);
	printf("Bandwidth limited tasks: %lu\n", stats->bandwidth_limited_tasks);
	printf("DAMON updates:           %lu\n", stats->damon_updates);
	printf("CXL migrations:          %lu\n", stats->cxl_migrations);
	printf("================================\n");
	
	prev_stats = *stats;
}

static void *monitor_thread(void *arg)
{
	struct scheduler_stats stats;
	
	while (!exit_req) {
		sleep(config.monitor_interval_sec);
		
		if (exit_req)
			break;
		
		memset(&stats, 0, sizeof(stats));
		
		if (config.verbose)
			print_stats(&stats);
	}
	
	return NULL;
}

static void print_usage(const char *prog)
{
	fprintf(stderr, help_fmt, prog, prog, prog, prog, prog);
}

static void parse_args(int argc, char **argv)
{
	static struct option long_options[] = {
		{"help",            no_argument,       0, 'h'},
		{"verbose",         no_argument,       0, 'v'},
		{"slice-us",        required_argument, 0, 's'},
		{"nr-cpus",         required_argument, 0, 'n'},
		{"read-bw",         required_argument, 0, 'r'},
		{"write-bw",        required_argument, 0, 'w'},
		{"disable-damon",   no_argument,       0, 'd'},
		{"disable-cxl",     no_argument,       0, 'c'},
		{"disable-bw-ctrl", no_argument,       0, 'b'},
		{"monitor",         required_argument, 0, 'm'},
		{"damon-path",      required_argument, 0, 'D'},
		{0, 0, 0, 0}
	};
	
	int opt;
	while ((opt = getopt_long(argc, argv, "hvs:n:r:w:dcbm:D:", long_options, NULL)) != -1) {
		switch (opt) {
		case 'h':
			print_usage(argv[0]);
			exit(0);
		case 'v':
			config.verbose = true;
			break;
		case 's':
			config.slice_us = strtoul(optarg, NULL, 10);
			break;
		case 'n':
			config.nr_cpus = strtoul(optarg, NULL, 10);
			break;
		case 'r':
			config.max_read_bandwidth_mb = strtoul(optarg, NULL, 10);
			break;
		case 'w':
			config.max_write_bandwidth_mb = strtoul(optarg, NULL, 10);
			break;
		case 'd':
			config.enable_damon = false;
			break;
		case 'c':
			config.enable_cxl_aware = false;
			break;
		case 'b':
			config.enable_bandwidth_control = false;
			break;
		case 'm':
			config.monitor_interval_sec = strtoul(optarg, NULL, 10);
			break;
		case 'D':
			config.damon_path = optarg;
			break;
		default:
			print_usage(argv[0]);
			exit(1);
		}
	}
	
	if (config.nr_cpus == 0)
		config.nr_cpus = get_nprocs();
	
	if (config.nr_cpus > MAX_CPUS) {
		fprintf(stderr, "Number of CPUs exceeds maximum (%d)\n", MAX_CPUS);
		exit(1);
	}
}

int main(int argc, char **argv)
{
	// struct scx_init_opts init_opts = {
	//	.exit_dump_len = 16384,
	// };
	struct bpf_link *link;
	pthread_t monitor_tid;
	int err = 0;
	
	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);
	libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
	
	parse_args(argc, argv);
	
	/* SCX_OPS_OPEN references newer struct_ops fields not emitted by this host's bpftool. */
	skel = scx_cxl__open();
	SCX_BUG_ON(!skel, "Could not open scx_cxl");
	skel->struct_ops.cxl_ops->hotplug_seq = scx_hotplug_seq();
	SCX_ENUM_INIT(skel);
	
	skel->rodata->nr_cpus = config.nr_cpus;
	skel->rodata->slice_ns = config.slice_us * 1000;
	skel->rodata->max_read_bandwidth_mb = config.max_read_bandwidth_mb;
	skel->rodata->max_write_bandwidth_mb = config.max_write_bandwidth_mb;
	skel->rodata->enable_damon = config.enable_damon;
	skel->rodata->enable_cxl_aware = config.enable_cxl_aware;
	skel->rodata->enable_bandwidth_control = config.enable_bandwidth_control;
	
	SCX_OPS_LOAD(skel, cxl_ops, scx_cxl, uei);
	
	if (setup_damon(config.damon_path) < 0) {
		fprintf(stderr, "Failed to setup DAMON, continuing without it\n");
		config.enable_damon = false;
	}
	
	link = SCX_OPS_ATTACH(skel, cxl_ops, scx_cxl);
	
	printf("CXL Bandwidth-Aware Scheduler Started\n");
	printf("Configuration:\n");
	printf("  CPUs: %u\n", config.nr_cpus);
	printf("  Slice: %lu us\n", config.slice_us);
	printf("  Max read bandwidth: %lu MB/s\n", config.max_read_bandwidth_mb);
	printf("  Max write bandwidth: %lu MB/s\n", config.max_write_bandwidth_mb);
	printf("  DAMON: %s\n", config.enable_damon ? "enabled" : "disabled");
	printf("  CXL-aware: %s\n", config.enable_cxl_aware ? "enabled" : "disabled");
	printf("  Bandwidth control: %s\n", config.enable_bandwidth_control ? "enabled" : "disabled");
	printf("\n");
	
	if (config.verbose) {
		if (pthread_create(&monitor_tid, NULL, monitor_thread, NULL) != 0) {
			fprintf(stderr, "Failed to create monitor thread\n");
		}
	}
	
	while (!exit_req && !UEI_EXITED(skel, uei))
		sleep(1);
	
	if (config.verbose)
		pthread_cancel(monitor_tid);
	
	cleanup_damon(config.damon_path);
	
	bpf_link__destroy(link);
	UEI_REPORT(skel, uei);
	scx_cxl__destroy(skel);
	
	// if (UEI_ECODE(skel, uei))
	//	err = UEI_ECODE(skel, uei);
	
	return err;
}
