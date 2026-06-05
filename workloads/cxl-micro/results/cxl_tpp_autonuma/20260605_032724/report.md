# CXL TPP and AutoNUMA Benchmark

- real CXL node visible: `False`
- local nodes: `0`
- CXL/slow-memory nodes: `none`
- placement policy: `kernel`
- buffer size: `1 GiB`
- duration per point: `3s`
- kernel knobs before run: `{"/proc/sys/kernel/numa_balancing": "0", "/proc/sys/kernel/numa_balancing_promote_rate_limit_MBps": "65536", "/sys/kernel/mm/numa/demotion_enabled": "false"}`

**CXL warning:** this boot exposes only local memory, so these numbers are smoke-test data rather than real CXL/TPP results.

## Summary

| policy | successful points | mean bandwidth GB/s | best bandwidth GB/s | peak local MB | peak CXL MB |
|---|---:|---:|---:|---:|---:|
| default | 6 | 25.613 | 39.581 | 1029.4 | 0.0 |
| autonuma | 6 | 25.615 | 39.557 | 1029.4 | 0.0 |
| tpp | 6 | 25.517 | 39.313 | 1029.4 | 0.0 |

## Artifacts

- `results.csv`
- `results.json`
- `results.jsonl`
- `numa_maps_samples.jsonl`
- `bandwidth_by_policy.svg`
- `bandwidth_by_policy.pdf`
