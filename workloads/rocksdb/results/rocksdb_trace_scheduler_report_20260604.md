# RocksDB Trace Scheduler Results

Date: 2026-06-04

## Setup

- Workload: `workloads/rocksdb/tpcc_query_bench`
- Threads: 16
- Transactions per trace per run: 1,000,000
- Repetitions: 3
- Traces:
  - `new_order`: 13 reads / 12 writes per transaction
  - `payment`: 3 reads / 3 writes per transaction
  - `order_status`: 7 reads / 0 writes per transaction
  - `delivery`: 3 reads / 3 writes per transaction
  - `stock_level`: 33 reads / 0 writes per transaction
- Machine NUMA state during run: one visible NUMA node, node 0 only.

The workload is a real RocksDB API workload with TPCC-like transactions. It is not a production trace replay, but it exercises RocksDB `Get`, `Put`, and `WriteBatch` paths and includes SLUG read/write markers around those operations.

## Scheduler Groups

Nest / SLUG group:

- `default`
- `scx_nest`
- `scx_nest_slug_read`
- `scx_nest_slug_write`
- `scx_nest_slug`

General scheduler group:

- `default`
- `scx_prev`
- `scx_p2dq`
- `scx_rusty`

## Overall Result

Geomean TPS speedup vs default:

| Scheduler | Geomean |
|---|---:|
| `scx_nest` | 0.831x |
| `scx_nest_slug_read` | 0.933x |
| `scx_nest_slug_write` | 0.947x |
| `scx_nest_slug` | 0.967x |
| `scx_prev` | 0.951x |
| `scx_p2dq` | 0.849x |
| `scx_rusty` | 0.845x |

No tested scheduler produced a stable overall throughput improvement across the five RocksDB traces.

## Best Case By Trace

| Trace | Best tested scheduler | TPS delta vs default |
|---|---|---:|
| `new_order` | `scx_p2dq` | -4.17% |
| `payment` | `scx_nest_slug_read` | -5.18% |
| `order_status` | `scx_nest` | +12.32% |
| `delivery` | `scx_rusty` | -5.46% |
| `stock_level` | `scx_nest_slug` | +4.81% |

Positive cases were trace-specific:

- `order_status` read-only point lookup trace improved with plain `scx_nest`.
- `stock_level` read-heavy scan trace improved with `scx_nest_slug`.
- Mixed read/write traces (`new_order`, `payment`, `delivery`) did not improve under the tested schedulers.

## Artifacts

Nest / SLUG results:

- `workloads/rocksdb/results/trace_scheduler_20260604_195528/rocksdb_trace_scheduler_summary.csv`
- `workloads/rocksdb/results/trace_scheduler_20260604_195528/rocksdb_trace_scheduler_rows.csv`
- `workloads/rocksdb/results/trace_scheduler_20260604_195528/rocksdb_trace_scheduler_raw.json`
- `workloads/rocksdb/results/trace_scheduler_20260604_195528/rocksdb_trace_scheduler_comparison.pdf`
- `workloads/rocksdb/results/trace_scheduler_20260604_195528/rocksdb_trace_scheduler_comparison.png`

General scheduler results:

- `workloads/rocksdb/results/trace_scheduler_20260604_200038/rocksdb_trace_scheduler_summary.csv`
- `workloads/rocksdb/results/trace_scheduler_20260604_200038/rocksdb_trace_scheduler_rows.csv`
- `workloads/rocksdb/results/trace_scheduler_20260604_200038/rocksdb_trace_scheduler_raw.json`
- `workloads/rocksdb/results/trace_scheduler_20260604_200038/rocksdb_trace_scheduler_comparison.pdf`
- `workloads/rocksdb/results/trace_scheduler_20260604_200038/rocksdb_trace_scheduler_comparison.png`

Runner:

- `workloads/rocksdb/run_trace_scheduler_comparison.py`

