# CXL Microbenchmark

Build the benchmark binary:

```bash
make -C workloads/cxl-micro
```

Compare the numactl baselines with the custom CXL-aware scheduler:

```bash
ulimit -l unlimited
python3 workloads/cxl-micro/run_numactl_scx_cxl_bench.py
```

The harness runs the same `double_bandwidth` workload matrix under:

- `numactl_local`: `numactl --interleave=<local DRAM nodes>`
- `numactl_cxl`: `numactl --interleave=<CXL memory nodes>`
- `numactl_local_cxl`: `numactl --interleave=<local DRAM + CXL nodes>`
- `scx_cxl`: custom CXL-aware sched-ext scheduler, using local+CXL interleave by default

On the current CXL test host, node `0` is the local CPU/DRAM node and node `1`
is the memory-only CXL node. The harness auto-detects that layout, but it can be
overridden:

```bash
python3 workloads/cxl-micro/run_numactl_scx_cxl_bench.py \
  --local-nodes 0 \
  --cxl-nodes 1 \
  --local-cxl-nodes 0,1
```

Quick validation run:

```bash
ulimit -l unlimited
python3 workloads/cxl-micro/run_numactl_scx_cxl_bench.py \
  --output-dir workloads/cxl-micro/results/numactl_scx_cxl/smoke \
  --buffer-size-gb 0.25 \
  --duration 1 \
  --thread-counts 4 \
  --read-ratios 0.5
```

