#!/usr/bin/env python3
"""Compare default Linux scheduling with the SLUG-selective Nest scheduler."""

import argparse
import csv
import json
import os
import signal
import statistics
import subprocess
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HINT_MAP = "/sys/fs/bpf/schedcp/slug_task_hints"


def run_cmd(cmd, env=None, timeout=900):
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return result


def start_slug_scheduler(results_dir, hint_map=DEFAULT_HINT_MAP):
    Path(hint_map).parent.mkdir(parents=True, exist_ok=True)
    stdout = open(results_dir / f"scx_nest_slug_{int(time.time() * 1000)}.stdout.log", "w")
    stderr = open(results_dir / f"scx_nest_slug_{int(time.time() * 1000)}.stderr.log", "w")
    cmd = [
        str(REPO_ROOT / "scheduler" / "sche_bin" / "scx_nest"),
        "-H",
        hint_map,
        "-N",
        "4",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        stdout=stdout,
        stderr=stderr,
        text=True,
        preexec_fn=os.setsid,
    )
    time.sleep(2)
    if proc.poll() is not None:
        stdout.close()
        stderr.close()
        raise RuntimeError(f"scx_nest_slug failed to start with code {proc.returncode}")
    return proc, stdout, stderr


def stop_scheduler(proc, stdout, stderr):
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    stdout.close()
    stderr.close()


def run_one(args, scheduler, rep, results_dir):
    env = os.environ.copy()
    env["SLUG_HINT_MAP"] = args.hint_map
    env.setdefault("OMP_PROC_BIND", "false")

    cmd = [
        str(REPO_ROOT / "workloads" / "hpc-memory" / "hpc_memory_kernels"),
        f"--kernel={args.kernel}",
        f"--threads={args.threads}",
        f"--size-mib={args.size_mib}",
        f"--iterations={args.iterations}",
        f"--slug-hint={args.slug_hint}",
    ]

    sched_proc = sched_stdout = sched_stderr = None
    if scheduler == "scx_nest_slug":
        sched_proc, sched_stdout, sched_stderr = start_slug_scheduler(results_dir, args.hint_map)

    try:
        result = run_cmd(cmd, env=env, timeout=args.timeout)
    finally:
        if scheduler == "scx_nest_slug":
            stop_scheduler(sched_proc, sched_stdout, sched_stderr)

    record = {
        "scheduler": scheduler,
        "rep": rep,
        "command": cmd,
        "return_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timestamp": datetime.now().isoformat(),
    }
    if result.returncode == 0:
        record["parsed"] = json.loads(result.stdout)
    return record


def flatten(records):
    rows = []
    for record in records:
        if record["return_code"] != 0:
            continue
        for entry in record["parsed"].get("results", []):
            rows.append({
                "scheduler": record["scheduler"],
                "rep": record["rep"],
                "kernel": entry["kernel"],
                "seconds": float(entry["seconds"]),
                "bandwidth_gb_s": float(entry["bandwidth_gb_s"]),
                "bytes": int(entry["bytes"]),
                "checksum": float(entry["checksum"]),
            })
    return rows


def summarize(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row["kernel"], row["scheduler"]), []).append(row)

    kernels = list(dict.fromkeys(row["kernel"] for row in rows))
    summary = []
    for kernel in kernels:
        default_rows = grouped.get((kernel, "default"), [])
        slug_rows = grouped.get((kernel, "scx_nest_slug"), [])
        if not default_rows or not slug_rows:
            continue
        default_bw = [r["bandwidth_gb_s"] for r in default_rows]
        slug_bw = [r["bandwidth_gb_s"] for r in slug_rows]
        default_sec = [r["seconds"] for r in default_rows]
        slug_sec = [r["seconds"] for r in slug_rows]
        default_bw_med = statistics.median(default_bw)
        slug_bw_med = statistics.median(slug_bw)
        default_sec_med = statistics.median(default_sec)
        slug_sec_med = statistics.median(slug_sec)
        summary.append({
            "kernel": kernel,
            "default_bandwidth_gb_s_median": default_bw_med,
            "slug_bandwidth_gb_s_median": slug_bw_med,
            "bandwidth_speedup": slug_bw_med / default_bw_med if default_bw_med else 0.0,
            "bandwidth_delta_pct": (slug_bw_med / default_bw_med - 1.0) * 100.0 if default_bw_med else 0.0,
            "default_seconds_median": default_sec_med,
            "slug_seconds_median": slug_sec_med,
            "time_speedup": default_sec_med / slug_sec_med if slug_sec_med else 0.0,
            "time_delta_pct": (slug_sec_med / default_sec_med - 1.0) * 100.0 if default_sec_med else 0.0,
        })
    return summary


def write_outputs(results_dir, records, rows, summary):
    with open(results_dir / "slug_allocator_comparison_raw.json", "w") as f:
        json.dump(records, f, indent=2)

    with open(results_dir / "slug_allocator_comparison_rows.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "scheduler", "rep", "kernel", "seconds", "bandwidth_gb_s", "bytes", "checksum"
        ])
        writer.writeheader()
        writer.writerows(rows)

    with open(results_dir / "slug_allocator_comparison_summary.csv", "w", newline="") as f:
        fieldnames = [
            "kernel",
            "default_bandwidth_gb_s_median",
            "slug_bandwidth_gb_s_median",
            "bandwidth_speedup",
            "bandwidth_delta_pct",
            "default_seconds_median",
            "slug_seconds_median",
            "time_speedup",
            "time_delta_pct",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)


def plot(results_dir, summary):
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return None, None

    kernels = [row["kernel"] for row in summary]
    x = np.arange(len(kernels))
    default_bw = [row["default_bandwidth_gb_s_median"] for row in summary]
    slug_bw = [row["slug_bandwidth_gb_s_median"] for row in summary]
    speedup = [row["bandwidth_speedup"] for row in summary]

    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(11, 8), height_ratios=[2.2, 1.0])
    width = 0.36
    ax0.bar(x - width / 2, default_bw, width, label="default")
    ax0.bar(x + width / 2, slug_bw, width, label="scx_nest_slug")
    ax0.set_ylabel("Median bandwidth (GB/s)")
    ax0.set_title("Default vs SLUG-selective scheduler on marker memory kernels")
    ax0.set_xticks(x)
    ax0.set_xticklabels(kernels, rotation=20, ha="right")
    ax0.grid(axis="y", alpha=0.3)
    ax0.legend()

    ax1.axhline(1.0, color="black", linewidth=1)
    ax1.bar(x, speedup, width=0.5, color="#4c78a8")
    ax1.set_ylabel("Speedup")
    ax1.set_xticks(x)
    ax1.set_xticklabels(kernels, rotation=20, ha="right")
    ax1.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    png = results_dir / "slug_allocator_comparison.png"
    pdf = results_dir / "slug_allocator_comparison.pdf"
    fig.savefig(png, dpi=300, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return png, pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--threads", type=int, default=48)
    parser.add_argument("--size-mib", type=int, default=512)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--kernel", default="all")
    parser.add_argument("--slug-hint", default="pipeline")
    parser.add_argument("--hint-map", default=DEFAULT_HINT_MAP)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--results-dir", default=None)
    args = parser.parse_args()

    results_dir = Path(args.results_dir or (
        REPO_ROOT / "workloads" / "hpc-memory" / "results" /
        f"slug_allocator_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ))
    results_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for rep in range(1, args.reps + 1):
        for scheduler in ("default", "scx_nest_slug"):
            print(f"rep {rep}/{args.reps}: {scheduler}", flush=True)
            record = run_one(args, scheduler, rep, results_dir)
            records.append(record)
            if record["return_code"] != 0:
                print(record["stderr"], flush=True)
                raise SystemExit(f"{scheduler} rep {rep} failed")
            time.sleep(1)

    rows = flatten(records)
    summary = summarize(rows)
    write_outputs(results_dir, records, rows, summary)
    png, pdf = plot(results_dir, summary)

    print(json.dumps({
        "results_dir": str(results_dir),
        "summary": summary,
        "png": str(png) if png else None,
        "pdf": str(pdf) if pdf else None,
    }, indent=2))


if __name__ == "__main__":
    main()
