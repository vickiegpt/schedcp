---
marp: true
theme: default
paginate: true
title: CXL Microbenchmark Scheduler Results
---

# CXL Microbenchmark Scheduler Results

## `numactl` baselines vs `scx_cxl`

- Repo: `schedcp`
- Workload: `workloads/cxl-micro/double_bandwidth`
- Result run: `workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/`
- Main question: does the custom CXL-aware scheduler improve bandwidth against local DRAM, CXL, and local+CXL interleave baselines?

---

# Executive Summary

- The custom scheduler ran successfully across the full matrix: `60/60` `scx_cxl` points succeeded.
- `scx_cxl` beat single-tier baselines:
  - `+45.5%` mean bandwidth vs local DRAM interleave only
  - `+77.9%` mean bandwidth vs CXL interleave only
- The strongest mean baseline was plain local+CXL interleave.
- `scx_cxl` trailed local+CXL interleave by `-5.1%` mean bandwidth.
- Practical message: the scheduler is stable and helpful versus single-tier placement, but it does not yet beat the best memory-placement baseline.

---

# Testbed Topology

NUMA layout on the benchmark host:

- Node `0`: CPU node with CPUs `0-47`
- Node `0` memory: `63,653 MB`
- Node `1`: memory-only node, used as CXL memory
- Node `1` memory: `124,874 MB`
- Distance from node `0` to node `1`: `50`

Interpretation:

- CPU execution is local to node `0`.
- CXL access is represented by memory allocated/interleaved on memory-only node `1`.

---

# Workload Matrix

Each point ran:

- Buffer size: `16 GiB`
- Duration: `10 seconds`
- Access pattern: random
- CPU bind: `numactl --cpunodebind=0`
- Read ratios:
  - `0.00, 0.15, 0.25, 0.35, 0.45, 0.50, 0.55, 0.65, 0.75, 0.85, 0.95, 1.00`
- Thread counts:
  - `4, 16, 64, 172, 256`

Total:

- `4` modes x `5` thread counts x `12` read ratios = `240` benchmark points

---

# Modes Compared

| Mode | Memory policy | Scheduler |
|---|---|---|
| `numactl_local` | `--interleave=0` | Linux default |
| `numactl_cxl` | `--interleave=1` | Linux default |
| `numactl_local_cxl` | `--interleave=0,1` | Linux default |
| `scx_cxl` | `--interleave=0,1` | custom `scx_cxl` |

`scx_cxl` was launched with:

```bash
scx_cxl -d -b
```

That keeps CXL-aware CPU scheduling enabled while disabling DAMON and bandwidth throttling for this run.

---

# Mean And Peak Bandwidth

| Mode | Success | Mean MB/s | Best MB/s | Best config |
|---|---:|---:|---:|---|
| Local DRAM | `60/60` | `22,643.4` | `35,784.7` | `256 threads`, read `1.00` |
| CXL | `60/60` | `18,521.9` | `22,623.9` | `16 threads`, read `1.00` |
| Local+CXL | `60/60` | `34,727.3` | `45,102.5` | `256 threads`, read `1.00` |
| `scx_cxl` | `60/60` | `32,951.2` | `45,081.1` | `256 threads`, read `0.95` |

Takeaway:

- Combining local DRAM and CXL memory tiers dominates single-tier placement.
- `scx_cxl` is close to local+CXL peak, but lower on average.

---

# Main Result Curve

<img src="./workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/bandwidth_by_read_ratio.svg" width="100%">

---

# Paired Comparison Against `scx_cxl`

For each matching `(threads, read_ratio)` point:

| Baseline | `scx_cxl` wins | `scx_cxl` losses | Geomean ratio | Mean delta |
|---|---:|---:|---:|---:|
| Local DRAM | `54` | `6` | `1.449x` | `+10,307.7 MB/s` |
| CXL | `60` | `0` | `1.745x` | `+14,429.2 MB/s` |
| Local+CXL | `2` | `58` | `0.947x` | `-1,776.2 MB/s` |

Interpretation:

- `scx_cxl` consistently beats either memory tier alone.
- Plain local+CXL interleave is still the stronger baseline for this memory-bandwidth microbenchmark.

---

# Thread Scaling Summary

Mean total bandwidth by thread count:

| Threads | Local DRAM | CXL | Local+CXL | `scx_cxl` |
|---:|---:|---:|---:|---:|
| `4` | `20,094.4` | `13,775.5` | `20,685.2` | `19,364.3` |
| `16` | `22,887.1` | `19,331.3` | `36,779.7` | `34,515.3` |
| `64` | `22,922.4` | `19,445.0` | `38,047.8` | `36,989.8` |
| `172` | `23,311.5` | `19,811.8` | `38,563.5` | `36,880.1` |
| `256` | `24,001.8` | `20,246.1` | `39,560.5` | `37,006.3` |

Takeaway:

- Local+CXL and `scx_cxl` both scale strongly once there are at least `16` threads.
- The scheduler gap versus local+CXL is persistent, not isolated to one thread count.

---

# What The Scheduler Result Means

`scx_cxl` is doing CPU scheduling, not memory placement.

In this experiment:

- memory placement is still handled by `numactl --interleave=0,1`,
- the scheduler changes how runnable tasks are selected and dispatched,
- bandwidth control was disabled,
- DAMON integration was disabled.

That framing matters:

- The best result came from memory placement across both tiers.
- Scheduler policy alone did not overcome the local+CXL interleave baseline for this synthetic bandwidth workload.

---

# Why Local+CXL Interleave Wins Here

Likely explanation from the data:

- The microbenchmark is bandwidth-dominated and highly parallel.
- Interleaving across both memory tiers increases available aggregate memory bandwidth.
- The workload has no service-level latency target or task priority mix.
- The custom scheduler adds policy, but this workload mostly rewards memory placement and raw bandwidth.

This does not invalidate the scheduler:

- It shows the scheduler is stable under load.
- It shows the scheduler improves over single-tier placement.
- It also identifies the stronger baseline that future scheduler versions must beat.

---

# Caveats

- This is a synthetic memory bandwidth benchmark, not an application-level service.
- The run used random access only.
- `double_bandwidth` rounds reader counts down at low thread counts; for example, low read ratios with `4` threads can produce `0` reader threads.
- `scx_cxl` was tested with DAMON and bandwidth throttling disabled.
- Results are from one host topology:
  - CPU node `0`
  - memory-only CXL node `1`

---

# Recommended Next Experiments

- Run the same matrix with sequential access.
- Re-run `scx_cxl` with bandwidth control enabled and tuned limits.
- Evaluate application workloads where scheduling policy should matter more:
  - FAISS / vector search
  - PyTorch UVM
  - vLLM or llama.cpp inference
- Add perf counters:
  - memory bandwidth by controller,
  - LLC miss rate,
  - cycles stalled on memory,
  - migrations and runqueue latency.

---

# Appendix: Artifacts

Benchmark harness:

- [run_numactl_scx_cxl_bench.py](/root/schedcp/workloads/cxl-micro/run_numactl_scx_cxl_bench.py:1)

Plotter:

- [plot_numactl_scx_cxl_results.py](/root/schedcp/workloads/cxl-micro/plot_numactl_scx_cxl_results.py:1)

Results:

- [results.csv](/root/schedcp/workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/results.csv)
- [results.json](/root/schedcp/workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/results.json)
- [bandwidth_by_read_ratio.svg](/root/schedcp/workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/bandwidth_by_read_ratio.svg)

