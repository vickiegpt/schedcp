# Read/Write Mixed Trace Candidates

Local status:

- This repository does not currently contain a ready-to-replay application or block trace file for RocksDB.
- The checked-out RocksDB source includes `trace_replay/`, trace analyzers, and `db_bench --benchmarks=replay` support, but no bundled production trace data.
- Existing local mixed workloads are synthetic: `db_bench` `readwhilewriting`, sysbench `oltp_read_write` docs, and the new `tpcc_query_bench` in this directory.

RocksDB-native option:

- RocksDB tracing records application-level query operations such as `Get`, `WriteBatch` operations, iterator seeks, and `MultiGet`.
- A useful next step is to run `tpcc_query_bench` or Redis/NGINX-style services with RocksDB tracing enabled, then replay the trace with `db_bench --benchmarks=replay --trace_file=...`.
- This is the best trace shape for SLUG read/write markers because it preserves KV-level read/write query type instead of only block I/O direction.

External public trace candidates:

- SNIA IOTTA repository: broad storage I/O trace repository intended for storage research. Useful for block-level read/write replay.
  Source: https://www.snia.org/educational-library/iotta-repository-2019
- cacheMon/cache_dataset: aggregates public cache/block traces, including Tencent Cloud EBS and Alibaba Cloud EBS. Tencent traces expose `IOType` with read/write values, and Alibaba traces expose `opcode` as `R`/`W`.
  Source: https://github.com/cacheMon/cache_dataset
- FIU/VISA traces: block-level traces for webserver, Moodle, file-server, and CloudVPS workloads. Useful for realistic mixed server I/O.
  Source: https://visa.lab.asu.edu/web/resources/traces/
- DOE/Recorder HPC application I/O traces: HPC simulation traces with HDF5, MPI-IO, and POSIX operations plus operation parameters such as file, offset, and flags.
  Source: https://www.osti.gov/biblio/1785979

Recommendation:

1. Use the new local `tpcc_query_bench` for scheduler-facing per-query read/write experiments now.
2. For trace replay, start with cacheMon Tencent/Alibaba EBS traces because the read/write direction is explicit and easy to convert to fio or a RocksDB synthetic driver.
3. For application-level RocksDB scheduling, generate a RocksDB native trace from a real service or from `tpcc_query_bench`; that keeps the read/write distinction closest to the SLUG marker semantics.
