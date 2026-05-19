#!/usr/bin/env python3
"""
Compare CXL microbenchmark workloads across numactl baselines and scx_cxl.

The benchmark workload matrix is the cartesian product of thread counts,
read ratios, and access patterns. Memory placement for the baseline modes is
controlled with numactl --interleave. The scx_cxl mode starts the custom
sched-ext scheduler once, then runs the same workload matrix, using the
local+CXL interleave policy by default so memory placement is comparable.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_THREAD_COUNTS = [4, 16, 64, 172, 256]
DEFAULT_READ_RATIOS = [0.0, 0.15, 0.25, 0.35, 0.45, 0.5, 0.55, 0.65, 0.75, 0.85, 0.95, 1.0]
DEFAULT_MODES = ["numactl_local", "numactl_cxl", "numactl_local_cxl", "scx_cxl"]
DEFAULT_ACCESS_PATTERNS = ["random"]


@dataclass(frozen=True)
class Mode:
    name: str
    label: str
    interleave_nodes: str
    uses_scheduler: bool = False


def parse_int_list(value: str) -> List[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_float_list(value: str) -> List[float]:
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def parse_access_patterns(value: str) -> List[str]:
    patterns = [part.strip().lower() for part in value.split(",") if part.strip()]
    invalid = [pattern for pattern in patterns if pattern not in {"random", "sequential"}]
    if invalid:
        raise ValueError(f"invalid access pattern(s): {', '.join(invalid)}")
    return patterns


def run_text_command(cmd: Sequence[str], timeout: int = 10) -> str:
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed.stdout


def detect_numa_nodes() -> Dict[str, str]:
    """Detect local CPU nodes and memory-only nodes from numactl output."""
    try:
        output = run_text_command(["numactl", "--hardware"])
    except Exception:
        return {"local": "0", "cxl": "1", "local_cxl": "0,1"}

    nodes: Dict[int, Dict[str, object]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("node ") or " cpus:" not in line and " size:" not in line:
            continue

        parts = line.split()
        if len(parts) < 3:
            continue

        try:
            node_id = int(parts[1])
        except ValueError:
            continue

        node_info = nodes.setdefault(node_id, {"cpus": [], "size_mb": 0})
        if parts[2] == "cpus:":
            node_info["cpus"] = parts[3:]
        elif parts[2] == "size:" and len(parts) >= 4:
            try:
                node_info["size_mb"] = int(parts[3])
            except ValueError:
                pass

    memory_nodes = [node for node, info in sorted(nodes.items()) if int(info.get("size_mb", 0)) > 0]
    cpu_nodes = [node for node, info in sorted(nodes.items()) if info.get("cpus")]
    memory_only_nodes = [node for node in memory_nodes if node not in cpu_nodes]

    local_nodes = cpu_nodes or ([memory_nodes[0]] if memory_nodes else [0])
    cxl_nodes = memory_only_nodes or [node for node in memory_nodes if node not in local_nodes] or local_nodes

    local = ",".join(str(node) for node in local_nodes)
    cxl = ",".join(str(node) for node in cxl_nodes)
    local_cxl = ",".join(str(node) for node in sorted(set(local_nodes + cxl_nodes)))
    return {"local": local, "cxl": cxl, "local_cxl": local_cxl}


def build_modes(args: argparse.Namespace) -> Dict[str, Mode]:
    detected = detect_numa_nodes()
    local_nodes = args.local_nodes or detected["local"]
    cxl_nodes = args.cxl_nodes or detected["cxl"]
    local_cxl_nodes = args.local_cxl_nodes or detected["local_cxl"]

    return {
        "numactl_local": Mode("numactl_local", "numactl interleave local DRAM", local_nodes),
        "numactl_cxl": Mode("numactl_cxl", "numactl interleave CXL", cxl_nodes),
        "numactl_local_cxl": Mode("numactl_local_cxl", "numactl interleave local DRAM + CXL", local_cxl_nodes),
        "scx_cxl": Mode("scx_cxl", "scx_cxl scheduler with local DRAM + CXL interleave", local_cxl_nodes, True),
    }


def ensure_executable(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"{label} is not executable: {path}")


def truncate_text(value: str, limit: int = 4096) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} bytes ..."


def make_workload_command(
    binary: Path,
    buffer_size_bytes: int,
    threads: int,
    duration: int,
    read_ratio: float,
    access_pattern: str,
) -> List[str]:
    cmd = [
        str(binary),
        "--buffer-size",
        str(buffer_size_bytes),
        "--threads",
        str(threads),
        "--duration",
        str(duration),
        "--read-ratio",
        str(read_ratio),
        "--json",
        "--no-numa",
    ]
    cmd.append("--random" if access_pattern == "random" else "--sequential")
    return cmd


def wrap_with_numactl(cmd: Sequence[str], cpu_node: int, interleave_nodes: str) -> List[str]:
    return [
        "numactl",
        f"--cpunodebind={cpu_node}",
        f"--interleave={interleave_nodes}",
        *cmd,
    ]


def start_scheduler(
    scheduler_bin: Path,
    scheduler_args: Sequence[str],
    output_dir: Path,
    wait_seconds: float,
) -> subprocess.Popen:
    log_path = output_dir / "scx_cxl_scheduler.log"
    log_handle = log_path.open("a")
    cmd = [str(scheduler_bin), *scheduler_args]
    proc = subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(scheduler_bin.parent),
        preexec_fn=os.setsid,
    )
    proc._schedcp_log_handle = log_handle  # type: ignore[attr-defined]
    time.sleep(wait_seconds)
    if proc.poll() is not None:
        log_handle.flush()
        log_handle.close()
        raise RuntimeError(f"scx_cxl exited during startup; see {log_path}")
    return proc


def stop_scheduler(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
    finally:
        log_handle = getattr(proc, "_schedcp_log_handle", None)
        if log_handle:
            log_handle.close()


def write_json(rows: Iterable[Dict[str, object]], path: Path) -> None:
    with path.open("w") as handle:
        json.dump(list(rows), handle, indent=2)


def write_csv(rows: Iterable[Dict[str, object]], path: Path) -> None:
    rows = list(rows)
    if not rows:
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def append_jsonl(row: Dict[str, object], path: Path) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def run_one(command: Sequence[str], timeout: int) -> Dict[str, object]:
    started_at = time.time()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "returncode": -1,
            "error": f"timeout after {timeout}s",
            "wall_clock_s": time.time() - started_at,
        }

    row: Dict[str, object] = {
        "status": "success" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "wall_clock_s": time.time() - started_at,
    }

    if completed.returncode != 0:
        row["error"] = truncate_text(completed.stderr.strip() or completed.stdout.strip() or "benchmark failed")
        row["stdout"] = truncate_text(completed.stdout)
        row["stderr"] = truncate_text(completed.stderr)
        return row

    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        row["status"] = "failed"
        row["error"] = f"failed to parse JSON output: {exc}"
        row["stdout"] = truncate_text(completed.stdout)
        row["stderr"] = truncate_text(completed.stderr)
        return row

    row.update(parsed)
    return row


def print_summary(rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    print("\nSummary")
    print("=======")
    for mode in sorted({str(row["mode"]) for row in rows}):
        mode_rows = [row for row in rows if row.get("mode") == mode]
        successes = [row for row in mode_rows if row.get("status") == "success"]
        failures = len(mode_rows) - len(successes)
        if not successes:
            print(f"{mode}: 0/{len(mode_rows)} success, {failures} failed")
            continue
        bandwidths = [float(row.get("total_bandwidth_mbps", 0)) for row in successes]
        mean_bw = sum(bandwidths) / len(bandwidths)
        best = max(successes, key=lambda row: float(row.get("total_bandwidth_mbps", 0)))
        print(
            f"{mode}: {len(successes)}/{len(mode_rows)} success, {failures} failed, "
            f"mean={mean_bw:.1f} MB/s, best={float(best.get('total_bandwidth_mbps', 0)):.1f} MB/s "
            f"(threads={best.get('num_threads')}, read_ratio={best.get('read_ratio')}, "
            f"pattern={best.get('access_pattern')})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark numactl CXL baselines against scx_cxl.")
    parser.add_argument("--binary", default=str(SCRIPT_DIR / "double_bandwidth"), help="Path to double_bandwidth")
    parser.add_argument("--scheduler-bin", default=str(REPO_ROOT / "scheduler/custom_schedulers/scx_cxl"), help="Path to scx_cxl")
    parser.add_argument("--output-dir", default="", help="Output directory; defaults to timestamped results dir")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES), help=f"Comma-separated modes: {', '.join(DEFAULT_MODES)}")
    parser.add_argument("--thread-counts", default=",".join(str(value) for value in DEFAULT_THREAD_COUNTS), help="Comma-separated thread counts")
    parser.add_argument("--read-ratios", default=",".join(str(value) for value in DEFAULT_READ_RATIOS), help="Comma-separated read ratios")
    parser.add_argument("--access-patterns", default=",".join(DEFAULT_ACCESS_PATTERNS), help="Comma-separated access patterns: random,sequential")
    parser.add_argument("--buffer-size-gb", type=float, default=16.0, help="Buffer size per run in GiB")
    parser.add_argument("--duration", type=int, default=10, help="Duration per run in seconds")
    parser.add_argument("--timeout", type=int, default=0, help="Timeout per run in seconds; default duration + 120")
    parser.add_argument("--cpu-node", type=int, default=0, help="CPU NUMA node for numactl --cpunodebind")
    parser.add_argument("--local-nodes", default="", help="Override local DRAM interleave nodes")
    parser.add_argument("--cxl-nodes", default="", help="Override CXL interleave nodes")
    parser.add_argument("--local-cxl-nodes", default="", help="Override local+CXL interleave nodes")
    parser.add_argument("--scheduler-args", default="-d -b", help="Arguments passed to scx_cxl; default disables DAMON and bandwidth throttling")
    parser.add_argument("--scheduler-start-wait", type=float, default=2.0, help="Seconds to wait after starting scx_cxl")
    args = parser.parse_args()

    binary = Path(args.binary).resolve()
    scheduler_bin = Path(args.scheduler_bin).resolve()
    ensure_executable(binary, "double_bandwidth")

    requested_modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    all_modes = build_modes(args)
    unknown_modes = [mode for mode in requested_modes if mode not in all_modes]
    if unknown_modes:
        raise ValueError(f"unknown mode(s): {', '.join(unknown_modes)}")

    if any(all_modes[mode].uses_scheduler for mode in requested_modes):
        ensure_executable(scheduler_bin, "scx_cxl")

    thread_counts = parse_int_list(args.thread_counts)
    read_ratios = parse_float_list(args.read_ratios)
    access_patterns = parse_access_patterns(args.access_patterns)
    buffer_size_bytes = int(args.buffer_size_gb * 1024**3)
    timeout = args.timeout if args.timeout > 0 else args.duration + 120

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = SCRIPT_DIR / "results" / "numactl_scx_cxl" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "results.csv"
    json_path = output_dir / "results.json"
    jsonl_path = output_dir / "results.jsonl"

    print(f"Output directory: {output_dir}")
    print(f"Modes: {requested_modes}")
    print(f"Thread counts: {thread_counts}")
    print(f"Read ratios: {read_ratios}")
    print(f"Access patterns: {access_patterns}")
    print(f"Buffer size: {args.buffer_size_gb:g} GiB")
    print(f"Duration: {args.duration}s")

    rows: List[Dict[str, object]] = []
    total_runs = len(requested_modes) * len(thread_counts) * len(read_ratios) * len(access_patterns)
    completed_runs = 0

    for mode_name in requested_modes:
        mode = all_modes[mode_name]
        scheduler_proc: Optional[subprocess.Popen] = None
        try:
            if mode.uses_scheduler:
                scheduler_args = shlex.split(args.scheduler_args)
                print(f"\nStarting scheduler: {scheduler_bin} {' '.join(scheduler_args)}")
                scheduler_proc = start_scheduler(scheduler_bin, scheduler_args, output_dir, args.scheduler_start_wait)

            print(f"\n== {mode.label} ({mode.name}, interleave={mode.interleave_nodes}) ==")
            for access_pattern in access_patterns:
                for threads in thread_counts:
                    for read_ratio in read_ratios:
                        completed_runs += 1
                        workload_cmd = make_workload_command(
                            binary=binary,
                            buffer_size_bytes=buffer_size_bytes,
                            threads=threads,
                            duration=args.duration,
                            read_ratio=read_ratio,
                            access_pattern=access_pattern,
                        )
                        command = wrap_with_numactl(workload_cmd, args.cpu_node, mode.interleave_nodes)
                        print(
                            f"[{completed_runs}/{total_runs}] mode={mode.name} "
                            f"pattern={access_pattern} threads={threads} read_ratio={read_ratio:.2f}"
                        )
                        row = run_one(command, timeout)
                        row.update(
                            {
                                "mode": mode.name,
                                "mode_label": mode.label,
                                "interleave_nodes": mode.interleave_nodes,
                                "cpu_node": args.cpu_node,
                                "threads_requested": threads,
                                "read_ratio_requested": read_ratio,
                                "access_pattern": access_pattern,
                                "buffer_size_gb_requested": args.buffer_size_gb,
                                "duration_requested": args.duration,
                                "command": " ".join(shlex.quote(part) for part in command),
                            }
                        )
                        rows.append(row)
                        append_jsonl(row, jsonl_path)
                        write_json(rows, json_path)
                        write_csv(rows, csv_path)

                        if row.get("status") == "success":
                            print(f"  total_bw={float(row.get('total_bandwidth_mbps', 0)):10.1f} MB/s")
                        else:
                            print(f"  {row.get('status')}: {row.get('error', 'unknown error')}")
        finally:
            stop_scheduler(scheduler_proc)

    print_summary(rows)
    print(f"\nWrote CSV:  {csv_path}")
    print(f"Wrote JSON: {json_path}")
    print(f"Wrote JSONL: {jsonl_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)
