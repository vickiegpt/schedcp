#!/usr/bin/env python3
"""Run RocksDB TPCC-like traces under default and sched_ext schedulers."""

import argparse
import csv
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ROCKSDB_DIR = REPO_ROOT / "workloads" / "rocksdb"
TPCC_BENCH = ROCKSDB_DIR / "tpcc_query_bench"
SCHED_BIN_DIR = REPO_ROOT / "scheduler" / "sche_bin"
SCHED_CONFIG = REPO_ROOT / "scheduler" / "schedulers.json"
DEFAULT_HINT_MAP = "/sys/fs/bpf/schedcp/slug_task_hints"
DEFAULT_TRACES = ["new_order", "payment", "order_status", "delivery", "stock_level"]
DEFAULT_SCHEDULERS = [
    "default",
    "scx_nest",
    "scx_nest_slug_read",
    "scx_nest_slug_write",
    "scx_nest_slug",
]


def load_scheduler_config():
    with open(SCHED_CONFIG) as f:
        cfg = json.load(f)
    schedulers = {}
    for entry in cfg.get("schedulers", []):
        name = entry.get("name")
        if not name:
            continue
        schedulers[name] = {
            "binary": entry.get("binary", name),
            "args": list(entry.get("args", [])),
        }
    return schedulers


def ensure_hint_dirs(args):
    for idx, arg in enumerate(args):
        if arg not in ("-H", "--slug-hint-map") or idx + 1 >= len(args):
            continue
        Path(args[idx + 1]).parent.mkdir(parents=True, exist_ok=True)


def start_scheduler(scheduler_name, schedulers, results_dir, wait=2.0):
    if scheduler_name == "default":
        return None
    if scheduler_name not in schedulers:
        raise RuntimeError(f"Unknown scheduler: {scheduler_name}")

    info = schedulers[scheduler_name]
    binary = SCHED_BIN_DIR / info["binary"]
    if not binary.exists():
        raise RuntimeError(f"Missing scheduler binary: {binary}")

    args = list(info.get("args", []))
    ensure_hint_dirs(args)
    stamp = int(time.time() * 1000)
    stdout = open(results_dir / f"{scheduler_name}_{stamp}.stdout.log", "w")
    stderr = open(results_dir / f"{scheduler_name}_{stamp}.stderr.log", "w")
    proc = subprocess.Popen(
        [str(binary)] + args,
        cwd=REPO_ROOT,
        stdout=stdout,
        stderr=stderr,
        text=True,
        preexec_fn=os.setsid,
    )
    time.sleep(wait)
    if proc.poll() is not None:
        stdout.close()
        stderr.close()
        err_path = results_dir / f"{scheduler_name}_{stamp}.stderr.log"
        err = err_path.read_text(errors="replace") if err_path.exists() else ""
        raise RuntimeError(
            f"{scheduler_name} failed to start with code {proc.returncode}: {err.strip()}"
        )
    return proc, stdout, stderr


def stop_scheduler(handle):
    if handle is None:
        return
    proc, stdout, stderr = handle
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    stdout.close()
    stderr.close()


def run_one(args, trace, scheduler_name, rep, schedulers, results_dir):
    db_path = Path(args.db_base) / f"{trace}_{scheduler_name}_rep{rep}"
    if db_path.exists():
        shutil.rmtree(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(TPCC_BENCH),
        f"--db={db_path}",
        f"--query={trace}",
        f"--transactions={args.transactions}",
        f"--threads={args.threads}",
        f"--warehouses={args.warehouses}",
        f"--districts={args.districts}",
        f"--customers={args.customers}",
        f"--items={args.items}",
        f"--order-lines={args.order_lines}",
        f"--value-size={args.value_size}",
    ]
    if args.numactl_membind is not None:
        cmd = ["numactl", "-m", str(args.numactl_membind)] + cmd

    env = os.environ.copy()
    env["SLUG_HINT_MAP"] = args.hint_map

    sched_handle = start_scheduler(scheduler_name, schedulers, results_dir)
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
    finally:
        stop_scheduler(sched_handle)
        if args.cleanup_db and db_path.exists():
            shutil.rmtree(db_path, ignore_errors=True)

    record = {
        "trace": trace,
        "scheduler": scheduler_name,
        "rep": rep,
        "command": cmd,
        "return_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "wall_seconds": time.time() - started,
        "timestamp": datetime.now().isoformat(),
    }
    if result.returncode == 0:
        json_start = result.stdout.find("{")
        record["parsed"] = json.loads(result.stdout[json_start:])
    return record


def flatten(records):
    rows = []
    for record in records:
        if record.get("return_code") != 0:
            continue
        for entry in record.get("parsed", {}).get("results", []):
            rows.append({
                "trace": record["trace"],
                "scheduler": record["scheduler"],
                "rep": record["rep"],
                "query": entry["query"],
                "transactions": int(entry["transactions"]),
                "seconds": float(entry["seconds"]),
                "tps": float(entry["tps"]),
                "avg_micros": float(entry["avg_micros"]),
                "read_ops_per_tx": float(entry["read_ops_per_tx"]),
                "write_ops_per_tx": float(entry["write_ops_per_tx"]),
                "errors": int(entry["errors"]),
                "wall_seconds": float(record["wall_seconds"]),
            })
    return rows


def summarize(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row["trace"], row["scheduler"]), []).append(row)

    traces = list(dict.fromkeys(row["trace"] for row in rows))
    schedulers = list(dict.fromkeys(row["scheduler"] for row in rows))
    summary = []
    for trace in traces:
        default_rows = grouped.get((trace, "default"), [])
        if not default_rows:
            continue
        default_tps = statistics.median(r["tps"] for r in default_rows)
        default_micros = statistics.median(r["avg_micros"] for r in default_rows)
        for scheduler in schedulers:
            sched_rows = grouped.get((trace, scheduler), [])
            if not sched_rows:
                continue
            tps_values = [r["tps"] for r in sched_rows]
            micros_values = [r["avg_micros"] for r in sched_rows]
            tps_med = statistics.median(tps_values)
            micros_med = statistics.median(micros_values)
            summary.append({
                "trace": trace,
                "scheduler": scheduler,
                "n": len(sched_rows),
                "read_ops_per_tx": sched_rows[0]["read_ops_per_tx"],
                "write_ops_per_tx": sched_rows[0]["write_ops_per_tx"],
                "median_tps": tps_med,
                "median_avg_micros": micros_med,
                "stdev_tps": statistics.stdev(tps_values) if len(tps_values) > 1 else 0.0,
                "tps_speedup_vs_default": tps_med / default_tps if default_tps else 0.0,
                "tps_delta_pct_vs_default": (tps_med / default_tps - 1.0) * 100.0 if default_tps else 0.0,
                "latency_delta_pct_vs_default": (micros_med / default_micros - 1.0) * 100.0 if default_micros else 0.0,
            })
    return summary


def geomean_speedups(summary):
    by_scheduler = {}
    for row in summary:
        if row["scheduler"] == "default":
            continue
        by_scheduler.setdefault(row["scheduler"], []).append(row["tps_speedup_vs_default"])
    return {
        scheduler: math.exp(sum(math.log(v) for v in values if v > 0.0) / len(values))
        for scheduler, values in by_scheduler.items()
        if values and all(v > 0.0 for v in values)
    }


def write_outputs(results_dir, records, rows, summary):
    with open(results_dir / "rocksdb_trace_scheduler_raw.json", "w") as f:
        json.dump(records, f, indent=2)

    row_fields = [
        "trace", "scheduler", "rep", "query", "transactions", "seconds", "tps",
        "avg_micros", "read_ops_per_tx", "write_ops_per_tx", "errors", "wall_seconds",
    ]
    with open(results_dir / "rocksdb_trace_scheduler_rows.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row_fields)
        writer.writeheader()
        writer.writerows(rows)

    summary_fields = [
        "trace", "scheduler", "n", "read_ops_per_tx", "write_ops_per_tx",
        "median_tps", "median_avg_micros", "stdev_tps",
        "tps_speedup_vs_default", "tps_delta_pct_vs_default",
        "latency_delta_pct_vs_default",
    ]
    with open(results_dir / "rocksdb_trace_scheduler_summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary)


def plot(results_dir, summary):
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return None, None

    traces = list(dict.fromkeys(row["trace"] for row in summary))
    schedulers = [
        s for s in dict.fromkeys(row["scheduler"] for row in summary)
        if s != "default"
    ]
    if not traces or not schedulers:
        return None, None

    x = np.arange(len(traces))
    width = min(0.82 / max(1, len(schedulers)), 0.18)
    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(13, 8), height_ratios=[2.0, 1.1])

    for idx, scheduler in enumerate(schedulers):
        offsets = x + (idx - (len(schedulers) - 1) / 2.0) * width
        speedups = []
        latencies = []
        for trace in traces:
            row = next(
                r for r in summary
                if r["trace"] == trace and r["scheduler"] == scheduler
            )
            speedups.append(row["tps_speedup_vs_default"])
            latencies.append(row["latency_delta_pct_vs_default"])
        ax0.bar(offsets, speedups, width, label=scheduler)
        ax1.bar(offsets, latencies, width, label=scheduler)

    ax0.axhline(1.0, color="black", linewidth=1)
    ax0.set_ylabel("TPS speedup vs default")
    ax0.set_title("RocksDB TPCC-like traces: scheduler speedup")
    ax0.set_xticks(x)
    ax0.set_xticklabels(traces, rotation=20, ha="right")
    ax0.grid(axis="y", alpha=0.3)
    ax0.legend(ncols=2)

    ax1.axhline(0.0, color="black", linewidth=1)
    ax1.set_ylabel("Latency delta (%)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(traces, rotation=20, ha="right")
    ax1.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    png = results_dir / "rocksdb_trace_scheduler_comparison.png"
    pdf = results_dir / "rocksdb_trace_scheduler_comparison.pdf"
    fig.savefig(png, dpi=300, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return png, pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", nargs="+", default=DEFAULT_TRACES)
    parser.add_argument("--schedulers", nargs="+", default=DEFAULT_SCHEDULERS)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--transactions", type=int, default=1_000_000)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--warehouses", type=int, default=4)
    parser.add_argument("--districts", type=int, default=8)
    parser.add_argument("--customers", type=int, default=1000)
    parser.add_argument("--items", type=int, default=10000)
    parser.add_argument("--order-lines", type=int, default=5)
    parser.add_argument("--value-size", type=int, default=128)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--hint-map", default=DEFAULT_HINT_MAP)
    parser.add_argument("--db-base", default="/tmp/rocksdb_trace_scheduler")
    parser.add_argument("--numactl-membind", default=None)
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()
    args.cleanup_db = not args.keep_db

    if not TPCC_BENCH.exists():
        raise SystemExit(f"Missing {TPCC_BENCH}; run make -C workloads/rocksdb tpcc-query-bench")

    schedulers = load_scheduler_config()
    results_dir = Path(args.results_dir or (
        ROCKSDB_DIR / "results" /
        f"trace_scheduler_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ))
    results_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for rep in range(1, args.reps + 1):
        for trace in args.traces:
            for scheduler_name in args.schedulers:
                print(
                    f"rep {rep}/{args.reps} trace={trace} scheduler={scheduler_name}",
                    flush=True,
                )
                record = run_one(args, trace, scheduler_name, rep, schedulers, results_dir)
                records.append(record)
                if record["return_code"] != 0:
                    write_outputs(results_dir, records, flatten(records), summarize(flatten(records)))
                    err = record.get("stderr", "").strip()
                    raise SystemExit(
                        f"{trace}/{scheduler_name}/rep{rep} failed with "
                        f"{record['return_code']}: {err}"
                    )
                time.sleep(args.pause)

    rows = flatten(records)
    summary = summarize(rows)
    write_outputs(results_dir, records, rows, summary)
    png, pdf = plot(results_dir, summary)
    print(json.dumps({
        "results_dir": str(results_dir),
        "geomean_speedups": geomean_speedups(summary),
        "png": str(png) if png else None,
        "pdf": str(pdf) if pdf else None,
    }, indent=2))


if __name__ == "__main__":
    main()
