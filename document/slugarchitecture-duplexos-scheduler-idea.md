# SLUG Architecture and DuplexOS Scheduler Idea

Date: 2026-06-04

This note documents the current SLUG implementation in this repo and a more
technical design direction for a DuplexOS scheduler. The core claim is:

> A useful production scheduler should not replace the Linux default policy with
> one global heuristic. It should run a conservative default-like policy for most
> execution and selectively switch policy only for short semantic phases whose
> behavior is visible to the application but mostly invisible to the kernel.

In the current repo, SLUG already provides the semantic signal path:

- application or benchmark marks read/write/balanced/pipeline basic blocks,
- markers write a per-thread hint into a pinned BPF map,
- `scx_nest` can consume that hint through `-H PATH` and `-N HINT`,
- only the selected hint receives warm-core Nest placement,
- all other hints bypass Nest and use a default-like CPU selection path.

DuplexOS is the next scheduler idea built on top of this: a two-plane scheduler
where the default/fair plane remains the safety baseline and a semantic plane
handles only proven beneficial phases.

## Current SLUG v0

### Name

SLUG can be read as "Scheduler-Level User Guidance". It is deliberately small:
the workload publishes a low-cardinality semantic hint, and the scheduler decides
whether to use it.

The current hint namespace is shared by C/C++ and Python marker libraries:

```c
enum slug_sched_hint {
        SLUG_SCHED_HINT_NONE = 0,
        SLUG_SCHED_HINT_READ = 1,
        SLUG_SCHED_HINT_WRITE = 2,
        SLUG_SCHED_HINT_BALANCED = 3,
        SLUG_SCHED_HINT_PIPELINE = 4,
};
```

Current default map path:

```text
/sys/fs/bpf/schedcp/slug_task_hints
```

### Data Path

The current v0 path is:

```text
instrumented basic block
        |
        v
SLUG_MARK_READ_BB() / SLUG_MARK_WRITE_BB() / ...
        |
        v
BPF_OBJ_GET("/sys/fs/bpf/schedcp/slug_task_hints")
        |
        v
BPF_MAP_UPDATE_ELEM(key = tid, value = hint)
        |
        v
scx_nest.bpf.c lookup on wakeup/select_cpu
        |
        +-- hint == slug_nest_hint -> Nest warm-core placement
        |
        +-- otherwise              -> scx_bpf_select_cpu_dfl fallback
```

Implementation references:

- C/C++ marker: `include/schedcp_slug_marker.h`
- Python marker: `include/schedcp_slug_marker.py`
- Scheduler BPF map and policy hook: `scheduler/scx/scheds/c/scx_nest.bpf.c`
- Scheduler CLI options: `scheduler/scx/scheds/c/scx_nest.c`
- Scheduler registry variants: `scheduler/schedulers.json`

### Marker Mechanics

The C/C++ marker library uses a direct `bpf()` syscall path instead of libbpf.
This keeps it embeddable inside existing workloads without extra linking:

```text
slug_mark_bb(hint)
        |
        +-- if thread-local cached_hint == hint: return 0
        |
        +-- fd = cached BPF_OBJ_GET(map_path)
        |
        +-- key = gettid()
        |
        +-- value = hint
        |
        +-- BPF_MAP_UPDATE_ELEM(fd, &key, &value, BPF_ANY)
```

Important details:

- The map fd is cached per thread with TLS.
- The last successfully written hint is cached per thread.
- A marker only does a map update when the hint changes.
- If the scheduler is not running or the map is absent, `BPF_OBJ_GET` fails and
  the marker degrades into a cheap failed hint path.
- Current key is `tid` as `u32`.
- The BPF scheduler also falls back to `tgid` lookup if `pid` lookup misses.

This is a transition marker, not a per-load/store marker. The intended use is:
mark the beginning of a semantic phase such as "this thread is now serving a
read request", not every memory access inside that request.

### Scheduler Mechanics

`scx_nest` currently has two SLUG flags in BPF rodata:

```c
const volatile bool slug_hint_map_enabled = false;
const volatile bool slug_selective_enabled = false;
const volatile u32 slug_nest_hint = SLUG_HINT_PIPELINE;
```

The user-space loader supports:

```text
-H PATH       Pin a SLUG read/write task hint map at PATH
-N HINT       Select which hint gets Nest placement
```

Current scheduler variants:

```text
scx_nest_slug       = scx_nest -H /sys/fs/bpf/schedcp/slug_task_hints -N 4
scx_nest_slug_read  = scx_nest -H /sys/fs/bpf/schedcp/slug_task_hints -N 1
scx_nest_slug_write = scx_nest -H /sys/fs/bpf/schedcp/slug_task_hints -N 2
```

The BPF map is:

```c
struct {
        __uint(type, BPF_MAP_TYPE_LRU_HASH);
        __uint(max_entries, 65536);
        __type(key, u32);
        __type(value, u32);
} slug_task_hints SEC(".maps");
```

The current decision point is `nest_select_cpu()`:

```c
if (!slug_should_use_nest(p)) {
        cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
        if (is_idle)
                scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_ns, 0);
        stat_inc(NEST_STAT(SLUG_BYPASS));
        return cpu;
}

stat_inc(NEST_STAT(SLUG_NEST));
/* continue into Nest primary/reserve placement */
```

So SLUG v0 is not a new full scheduler. It is a policy selector in front of
Nest:

- selected hint: use Nest warm-core packing,
- non-selected hint: use default-like SCX CPU selection.

That distinction matters. It is why the architecture is already closer to
DuplexOS than a monolithic replacement scheduler.

## Current Instrumentation Surface

### RocksDB

`workloads/rocksdb/tpcc_query_bench.cpp` wraps real RocksDB API operations:

```c
rocksdb::Status mark_get(...) {
        SLUG_MARK_READ_BB();
        return db->Get(...);
}

rocksdb::Status mark_put(...) {
        SLUG_MARK_WRITE_BB();
        return db->Put(...);
}

rocksdb::Status mark_write(...) {
        SLUG_MARK_WRITE_BB();
        return db->Write(...);
}
```

This is a good semantic boundary: RocksDB `Get`, `Put`, and `WriteBatch` are
high-level operations with clear read/write meaning. It is also a useful caution:
read/write semantics alone were not enough to improve mixed transactions.

### Redis

The local Redis tree has command-path markers in `server.c`:

```text
SLUG_MARK_PIPELINE_BB()
SLUG_MARK_WRITE_BB()
SLUG_MARK_READ_BB()
SLUG_MARK_BALANCED_BB()
```

This shape is appropriate for service workloads because the request classifier
knows more than the scheduler:

- read-only commands,
- write commands,
- mixed commands,
- pipelined requests.

### pyvsag

The pyvsag benchmark uses Python markers around build/add/search phases:

```text
slug_mark_write_bb()
slug_mark_balanced_bb()
slug_mark_read_bb()
```

This shows why the Python marker matters: many AI/search workloads have Python
orchestration around native compute kernels.

### CXL Microbench

`workloads/cxl-micro/thread_workers.hpp` marks read and write workers:

```text
SLUG_MARK_READ_BB()
SLUG_MARK_WRITE_BB()
```

This is useful for CXL experiments because placement policy should depend on
whether the current phase is latency-sensitive read, bandwidth-heavy write, or
mixed pointer chasing.

### HPC Memory Kernels

`workloads/hpc-memory/hpc_memory_kernels.cpp` accepts `--slug-hint=` and marks
OpenMP regions. This produced the cleanest positive signal because the workload
is phase-pure and marker overhead is amortized across large parallel regions.

## Empirical Lessons So Far

### Marker Memory Kernels

The `hpc-memory` SLUG allocator comparison used:

```text
default
scx_nest -H /sys/fs/bpf/schedcp/slug_task_hints -N 4
```

Configuration:

```text
5 reps, 48 threads, 512 MiB, 5 iterations, --slug-hint=pipeline
```

Median bandwidth result:

| Kernel | Default GB/s | SLUG GB/s | Delta |
|---|---:|---:|---:|
| `stream_triad` | 26.58 | 29.24 | +10.03% |
| `wrf_stencil` | 57.27 | 62.73 | +9.53% |
| `gromacs_pairlist` | 94.40 | 115.13 | +21.96% |
| `sst_sparse` | 15.22 | 16.34 | +7.34% |
| `quantum_state` | 29.37 | 35.26 | +20.04% |

Geomean bandwidth speedup: `1.136x`, or `+13.62%`.

Interpretation:

- Long, homogeneous, memory-heavy phases can benefit from selective warm-core
  packing.
- Marker overhead is small because each thread marks once per OpenMP region.
- The selected hint is stable long enough for scheduling policy to matter.

### RocksDB TPCC-like Traces

The RocksDB trace scheduler comparison used:

```text
16 threads, 1,000,000 transactions per trace per run, 3 reps
```

Traces:

| Trace | Reads / tx | Writes / tx |
|---|---:|---:|
| `new_order` | 13 | 12 |
| `payment` | 3 | 3 |
| `order_status` | 7 | 0 |
| `delivery` | 3 | 3 |
| `stock_level` | 33 | 0 |

Best result by trace across tested schedulers:

| Trace | Best tested scheduler | TPS delta vs default |
|---|---|---:|
| `new_order` | `scx_p2dq` | -4.17% |
| `payment` | `scx_nest_slug_read` | -5.18% |
| `order_status` | `scx_nest` | +12.32% |
| `delivery` | `scx_rusty` | -5.46% |
| `stock_level` | `scx_nest_slug` | +4.81% |

Geomean speedups:

| Scheduler | Geomean TPS speedup |
|---|---:|
| `scx_nest` | 0.831x |
| `scx_nest_slug_read` | 0.933x |
| `scx_nest_slug_write` | 0.947x |
| `scx_nest_slug` | 0.967x |
| `scx_prev` | 0.951x |
| `scx_p2dq` | 0.849x |
| `scx_rusty` | 0.845x |

Interpretation:

- SLUG/Nest is not a stable global win for mixed RocksDB transactions.
- Read-only traces can improve, but the winning policy differs by trace.
- Mixed read/write traces likely need deeper phase modeling than a single
  per-thread read/write hint.
- A production scheduler should avoid turning on warm-core packing globally for
  RocksDB just because read-heavy microbenchmarks improve.

This is the strongest argument for DuplexOS: the scheduler needs a conservative
fallback plane and a narrowly scoped semantic plane.

## Problem Statement

Linux scheduling already has good general-purpose behavior. The hard problem is
not replacing it. The hard problem is identifying short regions where the
application knows something that Linux does not:

- this thread is processing a read request,
- this thread is issuing write batches,
- this thread is in a search phase,
- this thread is a producer stage in a pipeline,
- this thread is a compaction or maintenance worker,
- this thread is latency critical for the next few milliseconds.

The kernel can infer some of this indirectly from sleeps, wakeups, CPU usage,
faults, and I/O waits. But it often cannot infer semantic intent soon enough.
By the time a heuristic detects the phase, the phase may be over.

SLUG moves phase identity into a low-overhead user-to-scheduler channel.

DuplexOS uses that channel without trusting it blindly.

## DuplexOS Scheduler Concept

### Definition

DuplexOS is a two-plane sched_ext scheduler:

```text
                         +----------------------+
                         |   user workload      |
                         | read/write markers   |
                         +----------+-----------+
                                    |
                                    v
                         +----------------------+
                         | SLUG hint map        |
                         | tid -> phase hint    |
                         +----------+-----------+
                                    |
                    +---------------+---------------+
                    |                               |
                    v                               v
        +----------------------+        +----------------------+
        | default/fair plane   |        | semantic plane       |
        | default-like policy  |        | phase-specific DSQs  |
        +----------+-----------+        +----------+-----------+
                   |                               |
                   +---------------+---------------+
                                   |
                                   v
                         +----------------------+
                         | CPU / LLC / NUMA     |
                         | placement decision   |
                         +----------------------+
```

The default/fair plane handles:

- unmarked tasks,
- unknown hints,
- hints with no proven benefit,
- kernel threads unless explicitly classified,
- background maintenance,
- all tasks when telemetry indicates semantic policy is hurting.

The semantic plane handles:

- selected read phases,
- selected write phases,
- pipeline phases,
- cgroup/service-specific critical paths,
- phases that pass duration and benefit thresholds.

### Core Principle

Policy should be selected by `(workload, phase, topology, pressure)`, not only by
`workload`.

```text
policy = f(comm, cgroup, hint, runq_depth, LLC_pressure, mem_bw, cpu_freq, SLO)
```

Current `scx_nest_slug` approximates this with:

```text
policy = Nest if hint == slug_nest_hint else default_like
```

DuplexOS generalizes it to:

```text
policy = policy_table[workload_class][hint][domain_state]
```

## DuplexOS Scheduler Planes

### Plane 0: Default/Fair Fallback

This plane should behave close to the kernel default for tasks that do not have
high-confidence semantic hints.

Implementation options:

- Use `scx_bpf_select_cpu_dfl()` for CPU selection where available.
- Insert directly into local DSQ when an idle CPU is selected.
- Use weighted vtime or FIFO fallback for queued work.
- Preserve previous CPU when useful.
- Avoid cross-LLC migration unless the local domain is overloaded.

This plane is not a second kernel scheduler. In sched_ext, the loaded scheduler
owns the class. "Default plane" means a default-like path inside the SCX
scheduler.

### Plane 1: Semantic Placement

The semantic plane implements hint-specific policy. Example initial table:

| Hint | Default placement | When to enable | When to bypass |
|---|---|---|---|
| `NONE` | default/fair | never | always |
| `READ` | previous CPU or LLC-local | read-only, cache-sensitive, moderate threads | high runq depth, poor cache reuse |
| `WRITE` | spread or isolate | writeback-heavy, dirty-cache isolation | write latency dominates, sync-heavy writes |
| `BALANCED` | default/fair | only if measured positive | mixed traces with short phases |
| `PIPELINE` | warm-core primary nest | long pipeline stages, low/moderate CPU utilization | high CPU utilization, bandwidth saturation |

The table must be workload-specific. RocksDB shows why: `stock_level` benefits
from pipeline/Nest, while `new_order`, `payment`, and `delivery` do not.

### Plane 2: Control and Adaptation

A user-space controller should tune the policy table while the BPF scheduler
keeps fast-path decisions bounded and verifier-friendly.

Controller inputs:

- SLUG stats: `SLUG_NEST`, `SLUG_BYPASS`,
- per-hint runtime,
- per-hint enqueue count,
- migrations,
- dispatch latency,
- task wait time,
- CPU frequency,
- LLC misses,
- memory bandwidth,
- application metrics: TPS, p50, p99, error rate.

Controller outputs:

- selected policy per hint,
- per-hint CPU domain caps,
- read/write weight ratio,
- latency class weights,
- safety disable flag for workloads where semantic policy is hurting.

## BPF Data Structures

Current v0:

```c
struct {
        __uint(type, BPF_MAP_TYPE_LRU_HASH);
        __uint(max_entries, 65536);
        __type(key, u32);   /* tid or tgid */
        __type(value, u32); /* slug hint */
} slug_task_hints SEC(".maps");
```

DuplexOS v1 should add:

```c
struct slug_task_state {
        u32 hint;
        u32 last_cpu;
        u32 domain_id;
        u32 flags;
        u64 hint_seq;
        u64 hint_ts_ns;
        u64 runnable_at_ns;
        u64 runtime_ns;
        u64 wait_ns;
};

struct slug_policy {
        u32 mode;             /* default, nest, spread, isolate, sticky */
        u32 max_cpus;         /* per-hint cap */
        u32 domain_mask_id;   /* LLC or NUMA domain */
        u32 latency_weight;
        u32 throughput_weight;
        u32 migration_penalty;
        u32 stale_hint_ttl_us;
};

struct slug_hint_stats {
        u64 enqueues;
        u64 dispatches;
        u64 bypasses;
        u64 migrations;
        u64 runtime_ns;
        u64 wait_ns;
};
```

Candidate maps:

```c
/* Published by userspace markers. */
BPF_MAP_TYPE_LRU_HASH slug_task_hints;       /* tid -> hint */

/* Owned by scheduler. */
BPF_MAP_TYPE_TASK_STORAGE slug_task_state;   /* task -> state */
BPF_MAP_TYPE_ARRAY slug_policy_table;        /* hint/domain -> policy */
BPF_MAP_TYPE_PERCPU_ARRAY slug_hint_stats;   /* hint -> counters */
BPF_MAP_TYPE_ARRAY slug_domain_masks;        /* domain_id -> cpumask ref */

/* Optional controller channel. */
BPF_MAP_TYPE_RINGBUF slug_events;            /* telemetry to controller */
```

Use `BPF_MAP_TYPE_TASK_STORAGE` for scheduler-owned state so task exit cleanup is
automatic. Keep the externally writable hint map as LRU hash for compatibility
with simple user markers.

## Hint Staleness and TID Reuse

Current v0 uses `tid -> hint` in an LRU map. That is simple, but has risks:

- a thread can exit without clearing its hint,
- a TID can be reused,
- a task can remain marked `READ` after transitioning to unmarked code if the
  application forgets `SLUG_MARK_NONE_BB()`,
- hint update is not a synchronous scheduling event.

Mitigations:

1. Add timestamp to value:

```c
struct slug_user_hint {
        u32 hint;
        u32 flags;
        u64 seq;
        u64 ts_ns;
};
```

2. Treat hints older than `stale_hint_ttl_us` as `NONE`.

3. Add optional marker API:

```c
SLUG_MARK_SCOPE_READ();
SLUG_MARK_SCOPE_WRITE();
SLUG_MARK_SCOPE_NONE();
```

In C++, RAII can guarantee reset:

```c++
class SlugScope {
public:
        explicit SlugScope(unsigned int hint) { slug_mark_bb(hint); }
        ~SlugScope() { slug_mark_bb(SLUG_SCHED_HINT_NONE); }
};
```

4. Add BPF tracepoint cleanup for task exit if keeping a pid-keyed map:

```text
sched_process_exit -> bpf_map_delete_elem(slug_task_hints, tid)
```

5. Prefer task storage inside the scheduler for all derived state.

## Scheduling Algorithm Sketch

### Enqueue

```c
void duplex_enqueue(task p, u64 enq_flags)
{
        state = get_task_state(p);
        hint = read_user_hint_or_none(p);
        policy = policy_lookup(p->cgroup, hint, current_domain_state());

        if (hint_is_stale(hint))
                hint = NONE;

        state->hint = hint;
        state->runnable_at_ns = now();

        if (policy.mode == DEFAULT)
                enqueue_default_plane(p, state, enq_flags);
        else
                enqueue_semantic_plane(p, state, policy, enq_flags);
}
```

### CPU Selection

```c
s32 duplex_select_cpu(task p, s32 prev_cpu, u64 wake_flags)
{
        state = get_task_state(p);
        policy = policy_for(state->hint);

        if (policy.mode == DEFAULT)
                return scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &idle);

        if (policy.mode == STICKY_READ)
                return select_prev_or_llc_local(p, prev_cpu, policy);

        if (policy.mode == NEST_PIPELINE)
                return select_primary_nest_or_reserve(p, prev_cpu, policy);

        if (policy.mode == SPREAD_WRITE)
                return select_least_loaded_domain_cpu(p, policy);

        if (policy.mode == ISOLATE_WRITE)
                return select_write_isolation_cpu(p, policy);

        return scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &idle);
}
```

### Dispatch

```c
void duplex_dispatch(s32 cpu, task prev)
{
        domain = cpu_to_domain(cpu);

        if (latency_read_budget_available(domain))
                consume(read_critical_dsq[domain]);

        if (pipeline_budget_available(domain))
                consume(pipeline_dsq[domain]);

        if (write_budget_available(domain))
                consume(write_dsq[domain]);

        consume(default_dsq[domain]);
        consume(global_default_dsq);
}
```

### Runtime Accounting

```c
void duplex_stopping(task p, bool runnable)
{
        state = get_task_state(p);
        delta = now() - state->last_start_ns;

        stats[state->hint].runtime_ns += delta;
        state->runtime_ns += delta;

        update_vtime_or_weighted_runtime(p, state->hint, delta);
}
```

## Policy Modes

### Default

Default-like path. Use this for:

- unmarked work,
- bad historical speedup,
- short phases,
- high system utilization,
- unknown workload.

### Sticky Read

Goal: preserve cache locality and avoid unnecessary migrations.

Placement:

1. attached CPU if idle,
2. previous CPU if allowed and not overloaded,
3. same LLC idle CPU,
4. fallback default.

Useful for:

- RocksDB point reads,
- vector index search,
- in-memory key lookup,
- cache resident read-mostly structures.

Risks:

- too much stickiness can cause runqueue imbalance,
- read phases that are actually memory bandwidth bound may need spreading.

### Nest Pipeline

Goal: keep selected long-running phase on warm cores to exploit boost frequency
and locality.

Placement:

1. primary nest,
2. reserve nest if primary saturated,
3. aggressive expansion after repeated placement failures.

Useful for:

- HPC memory kernels,
- pipelined Redis request handling when CPU utilization is moderate,
- read-heavy scans where compact placement improves cache/frequency behavior.

Risks:

- mixed read/write services can lose throughput,
- high CPU utilization prefers spreading,
- single-socket/single-CCX assumption matters.

### Spread Write

Goal: avoid dirty-cache and writeback contention on read-critical CPUs.

Placement:

1. write domain with spare bandwidth,
2. avoid read-critical primary cores,
3. avoid SMT sibling of read-critical task when possible,
4. fallback default.

Useful for:

- compaction,
- write batches,
- log append,
- background flush.

Risks:

- synchronous writes can be latency critical,
- spreading may hurt cache locality.

### Isolate Background

Goal: prevent maintenance tasks from interfering with request threads.

Candidate tasks:

- RocksDB compaction / flush workers,
- Redis active defrag,
- logging and metrics exporters,
- memory allocator purge threads.

Implementation:

- classify by `comm`, cgroup, or explicit SLUG hint,
- cap CPU share,
- avoid primary latency cores,
- allow stealing only when foreground is idle.

## DuplexOS For RocksDB

RocksDB needs more than read/write:

```text
foreground read request
foreground write request
write batch group leader
WAL write
memtable insert
memtable flush
L0/L1 compaction
block cache lookup
iterator seek
background prefetch
```

Current SLUG only sees `READ` and `WRITE` around `Get`, `Put`, and `WriteBatch`.
That is too coarse for mixed TPCC-like transactions.

Proposed RocksDB-specific hint expansion:

```c
enum slug_rocksdb_hint {
        SLUG_RDB_GET = 0x101,
        SLUG_RDB_ITER_SEEK = 0x102,
        SLUG_RDB_PUT = 0x201,
        SLUG_RDB_WRITE_BATCH = 0x202,
        SLUG_RDB_WAL_SYNC = 0x203,
        SLUG_RDB_MEMTABLE_INSERT = 0x204,
        SLUG_RDB_FLUSH = 0x301,
        SLUG_RDB_COMPACTION = 0x302,
};
```

Initial policy:

| RocksDB phase | Suggested policy |
|---|---|
| `GET` cache hit | sticky read |
| `GET` cache miss | default or spread read |
| `ITER_SEEK` | sticky read if cache resident, otherwise default |
| `PUT` | default |
| `WRITE_BATCH` | default or isolate if async |
| `WAL_SYNC` | do not pack blindly; latency depends on I/O |
| `FLUSH` | isolate background |
| `COMPACTION` | isolate background or spread write |

This explains the current result:

- `order_status` and `stock_level` are read-only enough to show positive cases.
- `new_order`, `payment`, and `delivery` interleave reads and writes; a single
  thread-level hint collapses multiple internal RocksDB phases into one policy.

## DuplexOS For Redis

Redis has a cleaner semantic classifier because commands are already known:

```text
GET/MGET/EXISTS        -> READ
SET/DEL/HSET/LPUSH     -> WRITE
EVAL/MULTI/mixed       -> BALANCED
pipelined batch        -> PIPELINE
background defrag      -> BACKGROUND
```

Policy idea:

- `READ`: sticky read or default if runqueue imbalance grows,
- `WRITE`: default or spread write,
- `PIPELINE`: Nest only if CPU utilization is below saturation,
- `BACKGROUND`: isolate and cap.

Metric target:

```text
maximize throughput subject to p99 latency not worse than default
```

Redis is a good DuplexOS candidate because the application-level command type is
stable for the duration of request handling.

## DuplexOS For Vector Search

Vector search has two sharply different phases:

```text
index build/add -> write/balanced, bandwidth heavy
query/search    -> read, cache/memory latency sensitive
```

Policy idea:

- Build/add: spread or default, avoid starving search threads.
- Search: sticky read within LLC, optionally Nest for short top-k compute.
- Mixed update/search services: put search on semantic read lane and add/build
  on write/background lane.

This is a stronger fit than RocksDB mixed write traces because search queries
usually stay in one phase for longer.

## DuplexOS For CXL

CXL memory introduces another dimension: local DRAM versus far memory.

Extend the hint:

```text
hint = phase | memory_tier | criticality
```

Example bit layout:

```c
#define SLUG_PHASE_MASK      0x000000ff
#define SLUG_TIER_MASK       0x00000f00
#define SLUG_CRITICAL_MASK   0x0000f000

#define SLUG_TIER_DRAM       0x00000100
#define SLUG_TIER_CXL        0x00000200
#define SLUG_CRITICAL_LOW    0x00001000
#define SLUG_CRITICAL_HIGH   0x00002000
```

CXL policy:

- CXL read latency sensitive: preserve LLC locality, avoid migrations.
- CXL streaming read: spread enough to fill memory bandwidth.
- CXL write: isolate if it interferes with DRAM read-critical tasks.
- Mixed DRAM/CXL: avoid placing CXL-heavy threads on cores serving DRAM
  low-latency reads.

## Cost Model

The scheduler should not blindly trust hints. Use a local objective:

```text
score(policy, phase) =
    throughput_gain
  - alpha * p99_latency_regression
  - beta  * migrations
  - gamma * LLC_miss_rate
  - delta * scheduler_overhead
  - eta   * energy_or_freq_penalty
```

Fast-path BPF should not compute this full score. Instead:

1. BPF exports counters.
2. User-space controller computes policy choices.
3. Controller writes compact policy table into BPF maps.
4. BPF fast path does table lookup and simple bounded decisions.

Minimum measured features:

```text
per-hint runtime_ns
per-hint wait_ns
per-hint migrations
per-hint dispatch count
per-hint bypass count
per-domain runq depth
cpu frequency
LLC misses
memory bandwidth
application TPS / p99
```

## Gating Rules

Initial safety gates:

```text
if hint == NONE:
        default

if hint_duration_p50 < 50 us:
        default

if scheduler_overhead / phase_runtime > 1%:
        default

if system_utilization > 85%:
        avoid Nest; prefer spread/default

if workload_geomean_speedup < 1.00 over last N windows:
        disable semantic plane for that workload

if p99_latency_regression > 5%:
        disable semantic plane for latency class
```

For RocksDB specifically:

```text
if trace is mixed read/write and no sub-phase hints exist:
        default

if read_ops_per_tx / max(1, write_ops_per_tx) > 8 and phase duration is stable:
        try read/sticky or pipeline/Nest

if background compaction is visible:
        isolate background, not foreground writes
```

## Marker Overhead Budget

Current marker overhead sources:

- one `BPF_OBJ_GET` per thread per run after first marker,
- one `BPF_MAP_UPDATE_ELEM` syscall per hint transition,
- one `gettid()` syscall per successful update,
- thread-local checks on every marker call.

Optimization directions:

1. Mark coarse phases, not inner loops.
2. Keep transition caching.
3. Add `slug_mark_bb_fast(hint, tid)` for callers that already know tid.
4. Add batch markers for worker pools.
5. Consider `rseq` or shared memory only if syscall cost dominates.

The syscall marker is acceptable for request/phase boundaries. It is not
acceptable for every key lookup inside a tight loop unless transitions are rare.

## Immediate Versus Delayed Hint Effect

Current hint updates do not force an immediate scheduler decision. The scheduler
observes the hint when the task is enqueued, woken, or selected. This is usually
fine for request phases that block or yield naturally, but weak for CPU-bound
code that changes phase while continuously running.

Possible solutions:

- accept delayed effect for coarse request phases,
- call marker before blocking/wakeup boundaries,
- add optional `SLUG_MARK_AND_YIELD_*` for long CPU-bound phase transitions,
- add timer-driven preemption in the scheduler and re-read hint on dispatch,
- use application worker pool handoff so each phase begins with an enqueue.

Do not force `sched_yield()` by default. It can destroy throughput.

## API Proposal

### C API

```c
int slug_init(const char *map_path);
int slug_mark_bb(unsigned int hint);
int slug_mark_scope_enter(unsigned int hint, unsigned int flags);
int slug_mark_scope_exit(void);
int slug_mark_thread_class(unsigned int class_id);
```

### C++ API

```c++
class SlugScope {
public:
        explicit SlugScope(unsigned int hint);
        ~SlugScope();
};

#define SLUG_READ_SCOPE()  SlugScope _slug_scope(SLUG_SCHED_HINT_READ)
#define SLUG_WRITE_SCOPE() SlugScope _slug_scope(SLUG_SCHED_HINT_WRITE)
```

### Python API

```python
with slug_scope(SLUG_HINT_READ):
    index.search(query)
```

### Extended Hints

Keep the first 8 bits common:

```text
0x00 none
0x01 read
0x02 write
0x03 balanced
0x04 pipeline
```

Use upper bits for domain-specific subtypes:

```text
bits  0..7   generic phase
bits  8..15  workload subtype
bits 16..23  memory tier / locality
bits 24..31  criticality / flags
```

This preserves compatibility with existing `-N 1`, `-N 2`, `-N 4` policies
while allowing richer controllers.

## DuplexOS Scheduler Implementation Roadmap

### v0: Current State

Already implemented:

- C/C++ SLUG marker header,
- Python SLUG marker,
- `scx_nest` SLUG map support,
- `scx_nest_slug*` scheduler registry entries,
- workload instrumentation in RocksDB, Redis, pyvsag, CXL microbench, HPC
  memory kernels,
- benchmark scripts and reports.

Missing:

- stale hint cleanup,
- phase duration telemetry,
- per-workload policy table,
- sub-phase hints for RocksDB,
- adaptive controller.

### v1: Robust Hint Channel

Tasks:

- replace `u32 hint` value with `{hint, seq, ts_ns, flags}`,
- add task-exit cleanup or TTL,
- expose `SLUG_MARK_NONE_BB()` in workload phase exits,
- add `SLUG_NEST` and `SLUG_BYPASS` export to benchmark logs,
- add marker overhead microbench.

Expected outcome:

- fewer stale classifications,
- clearer measurement of scheduler decisions,
- safer production fallback.

### v2: Hint-Aware Duplex Scheduler

Tasks:

- create `scx_duplex` or extend `scx_nest` into a general policy table,
- add per-hint DSQs,
- add default/fair plane,
- add semantic plane with policy modes:
  - default,
  - sticky read,
  - nest pipeline,
  - spread write,
  - isolate background,
- add per-hint stats.

Expected outcome:

- read-heavy traces can get specialized policy,
- mixed traces can stay default,
- background work can be isolated without hurting foreground.

### v3: Workload-Specific Policies

Tasks:

- Redis command classifier -> policy table,
- RocksDB internal phase hints beyond `Get` and `Write`,
- pyvsag add/search classifier,
- CXL memory-tier-aware hints.

Expected outcome:

- policies align to real service phases rather than generic read/write only.

### v4: Adaptive Controller

Tasks:

- collect BPF and application counters,
- run online A/B windows,
- compute per-workload policy score,
- update policy maps,
- disable semantic plane on regression.

Expected outcome:

- scheduler learns that RocksDB mixed traces should stay default,
- scheduler enables Nest only for phases like `stock_level` when positive,
- scheduler keeps p99 latency within a configured guardrail.

## Evaluation Plan

### Workloads

Use both synthetic and real-ish workloads:

```text
hpc-memory           phase-pure memory kernels
cxl-micro            read/write local/far memory workers
redis + memtier      command-level service workload
rocksdb TPCC-like    per-query KV traces
rocksdb db_bench     native RocksDB benchmark traces
pyvsag               vector add/search
nginx                request handling and static file paths
llama.cpp            decode/prefill phases if instrumented
```

### Metrics

Scheduler-level:

```text
SLUG_NEST
SLUG_BYPASS
per-hint runtime
per-hint wait time
migrations
runqueue depth
dispatch latency
DSQ occupancy
```

CPU/memory:

```text
CPU frequency
IPC
LLC misses
LLC references
memory bandwidth
NUMA local/remote accesses
CXL bandwidth if available
context switches
```

Application:

```text
throughput
p50/p95/p99 latency
error rate
tail amplification
build/query time split
```

### Ablations

```text
default
plain scx_nest
scx_nest_slug_read
scx_nest_slug_write
scx_nest_slug_pipeline
scx_duplex with semantic plane disabled
scx_duplex with one hint enabled at a time
scx_duplex with adaptive controller
```

### Required Plots

```text
speedup vs default by trace
p99 delta vs default by trace
SLUG_NEST / SLUG_BYPASS ratio
migrations per transaction
LLC misses per transaction
CPU frequency over time
policy selected over time
```

## Failure Modes

### Hint Is Too Coarse

Symptom:

```text
read-only microbench improves, mixed real workload regresses
```

Example:

```text
RocksDB new_order/payment/delivery
```

Fix:

- add sub-phase hints,
- restrict semantic policy to read-only or long phases,
- use default for mixed transactions.

### Hint Is Too Fine

Symptom:

```text
marker syscall overhead dominates runtime
```

Fix:

- mark request or region boundaries,
- cache transitions,
- do not mark every key or memory access,
- use scoped markers.

### Hint Is Stale

Symptom:

```text
task remains in read lane while doing write/background work
```

Fix:

- TTL,
- scope exit marker,
- task exit cleanup,
- controller disables suspicious long-lived hints.

### Policy Fights Hardware

Symptom:

```text
Nest packing hurts high-utilization or bandwidth-saturated workload
```

Fix:

- utilization gate,
- memory bandwidth gate,
- topology-specific policy,
- fallback default.

### Tail Latency Regresses

Symptom:

```text
throughput improves but p99 worsens
```

Fix:

- explicit p99 guardrail,
- latency lane budget,
- write/background isolation,
- disable semantic plane for affected cgroup.

## What To Build Next

Most valuable next patch series:

1. Add timestamped hint values and TTL.
2. Add per-hint stats export in `scx_nest` logs or a small reader.
3. Add `SLUG_MARK_SCOPE_*` helpers.
4. Add `scx_duplex` prototype:
   - default plane,
   - read lane,
   - write/background lane,
   - pipeline/Nest lane.
5. Add RocksDB internal hints for compaction, flush, WAL sync, and batch group.
6. Add controller that can disable semantic lanes per workload when median TPS
   or p99 latency regresses.

The key design rule is:

```text
Never enable the semantic plane globally just because one phase improves.
Enable it per workload, per phase, and per topology, with measured rollback.
```

## Short Thesis

SLUG is the annotation and transport layer. DuplexOS is the scheduler policy
that uses that transport safely.

The current results already show the split:

- phase-pure HPC memory kernels benefit strongly from SLUG-selective Nest,
- RocksDB mixed transactions do not benefit from global read/write/Nest policy,
- therefore the next scheduler should be duplex: conservative by default,
  aggressive only for phases with measured benefit.

