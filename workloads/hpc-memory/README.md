# HPC Memory-Bandwidth Proxy Workloads

This directory contains small, local proxy kernels for scheduler experiments on
memory-bandwidth-bound HPC patterns. They are not full application builds; they
are intended to be quick, reproducible stand-ins that can run under sched_ext
schedulers without downloading large benchmark suites.

Kernels:

- `stream_triad`: STREAM-style contiguous read/read/read/write bandwidth.
- `wrf_stencil`: 3D seven-point stencil proxy for WRF-like structured-grid weather kernels.
- `gromacs_pairlist`: neighbor-list gather/scatter proxy for GROMACS-like molecular dynamics force loops.
- `sst_sparse`: irregular sparse graph propagation proxy for SST-style simulation workloads.
- `quantum_state`: state-vector gate update proxy for quantum simulator memory sweeps.

Build:

```bash
make -C workloads/hpc-memory build
```

Quick scheduler run:

```bash
python3 workloads/hpc-memory/hpc_memory_benchmark.py \
  --quick \
  --results-dir workloads/hpc-memory/results/slug_hpc_memory \
  --schedulers scx_nest scx_nest_slug scx_nest_slug_read scx_nest_slug_write
```

The runner writes JSON plus `hpc_memory_scheduler_comparison.png` and
`hpc_memory_scheduler_comparison.pdf`.
