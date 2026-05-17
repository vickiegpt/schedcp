#!/usr/bin/env python3
"""
Run thread-scaling/read-ratio sweeps for local DRAM vs CXL memory and draw SVG graphs.

This script uses the local ``double_bandwidth`` binary only and depends on the
Python standard library, so it can run on stripped-down environments.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


DEFAULT_THREAD_COUNTS = [4, 16, 64, 172, 256]
DEFAULT_READ_RATIOS = [0.0, 0.15, 0.25, 0.35, 0.45, 0.5, 0.55, 0.65, 0.75, 0.85, 0.95, 1.0]
DEFAULT_BUFFER_SIZE_GB = 64
DEFAULT_DURATION = 10
DEFAULT_TIMEOUT = 600

PANEL_COLORS = [
    "#0e7490",
    "#dc2626",
    "#16a34a",
    "#7c3aed",
    "#ea580c",
    "#0891b2",
]


@dataclass(frozen=True)
class SweepConfig:
    name: str
    label: str
    cpu_node: int
    mem_node: int


def parse_int_list(value: str) -> List[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_float_list(value: str) -> List[float]:
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def format_gb(buffer_size_bytes: int) -> str:
    return f"{buffer_size_bytes / (1024 ** 3):.0f}GB"


def ensure_binary_exists(binary: Path) -> None:
    if not binary.exists():
        raise FileNotFoundError(f"Benchmark binary not found: {binary}")
    if not os.access(binary, os.X_OK):
        raise PermissionError(f"Benchmark binary is not executable: {binary}")


def run_benchmark(
    binary: Path,
    config: SweepConfig,
    buffer_size_bytes: int,
    threads: int,
    duration: int,
    read_ratio: float,
    random_access: bool,
    timeout: int,
) -> Dict[str, object]:
    cmd = [
        str(binary),
        "-N",
        str(config.cpu_node),
        "-M",
        str(config.mem_node),
        "--buffer-size",
        str(buffer_size_bytes),
        "--threads",
        str(threads),
        "--duration",
        str(duration),
        "--read-ratio",
        str(read_ratio),
        "--json",
        "--random" if random_access else "--sequential",
    ]

    started_at = time.time()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "error": f"timeout after {timeout}s",
            "command": " ".join(cmd),
            "config": config.name,
            "cpu_node": config.cpu_node,
            "mem_node": config.mem_node,
            "threads": threads,
            "read_ratio": read_ratio,
            "buffer_size_gb": buffer_size_bytes / (1024 ** 3),
            "duration": duration,
            "wall_clock_s": time.time() - started_at,
        }

    row: Dict[str, object] = {
        "status": "success" if completed.returncode == 0 else "failed",
        "command": " ".join(cmd),
        "config": config.name,
        "label": config.label,
        "cpu_node": config.cpu_node,
        "mem_node": config.mem_node,
        "threads": threads,
        "read_ratio": read_ratio,
        "buffer_size_gb": buffer_size_bytes / (1024 ** 3),
        "duration": duration,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "wall_clock_s": time.time() - started_at,
    }

    if completed.returncode != 0:
        row["error"] = completed.stderr.strip() or completed.stdout.strip() or "benchmark failed"
        return row

    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        row["status"] = "failed"
        row["error"] = f"failed to parse JSON output: {exc}"
        return row

    row.update(
        {
            "test_duration": parsed.get("test_duration", 0),
            "total_bandwidth_mbps": parsed.get("total_bandwidth_mbps", 0),
            "read_bandwidth_mbps": parsed.get("read_bandwidth_mbps", 0),
            "write_bandwidth_mbps": parsed.get("write_bandwidth_mbps", 0),
            "total_iops": parsed.get("total_iops", 0),
            "num_readers": parsed.get("num_readers", 0),
            "num_writers": parsed.get("num_writers", 0),
            "enable_numa": parsed.get("enable_numa", False),
            "numa_node": parsed.get("numa_node", config.cpu_node),
            "memory_numa_node": parsed.get("memory_numa_node", config.mem_node),
            "used_numa_alloc": parsed.get("used_numa_alloc", False),
            "random_access": parsed.get("random_access", random_access),
        }
    )
    return row


def write_csv(rows: Iterable[Dict[str, object]], csv_path: Path) -> None:
    rows = list(rows)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: Iterable[Dict[str, object]], json_path: Path) -> None:
    with json_path.open("w") as handle:
        json.dump(list(rows), handle, indent=2)


def group_success_rows(rows: Iterable[Dict[str, object]]) -> Dict[str, Dict[int, List[Dict[str, object]]]]:
    grouped: Dict[str, Dict[int, List[Dict[str, object]]]] = {}
    for row in rows:
        if row.get("status") != "success":
            continue
        config_name = str(row["config"])
        threads = int(row["threads"])
        grouped.setdefault(config_name, {}).setdefault(threads, []).append(row)

    for thread_map in grouped.values():
        for values in thread_map.values():
            values.sort(key=lambda item: float(item["read_ratio"]))
    return grouped


def nice_ceil(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    if normalized <= 1:
        step = 1
    elif normalized <= 2:
        step = 2
    elif normalized <= 5:
        step = 5
    else:
        step = 10
    return step * magnitude


def svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def make_svg(
    rows: Iterable[Dict[str, object]],
    svg_path: Path,
    configs: List[SweepConfig],
    thread_counts: List[int],
    buffer_size_gb: float,
    duration: int,
    random_access: bool,
) -> None:
    rows = list(rows)
    grouped = group_success_rows(rows)
    max_bw = max((float(row.get("total_bandwidth_mbps", 0)) for row in rows if row.get("status") == "success"), default=1.0)
    y_max = nice_ceil(max_bw * 1.1)

    width = 1500
    height = 760
    margin_left = 90
    margin_top = 90
    margin_bottom = 90
    panel_gap = 40
    panel_width = (width - margin_left - 80 - panel_gap) // 2
    panel_height = height - margin_top - margin_bottom
    plot_inner_left = 55
    plot_inner_top = 20
    plot_inner_right = 20
    plot_inner_bottom = 55

    def x_pos(panel_x: float, value: float) -> float:
        plot_w = panel_width - plot_inner_left - plot_inner_right
        return panel_x + plot_inner_left + (value / 1.0) * plot_w

    def y_pos(panel_y: float, value: float) -> float:
        plot_h = panel_height - plot_inner_top - plot_inner_bottom
        return panel_y + panel_height - plot_inner_bottom - (value / y_max) * plot_h

    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    parts.append('<rect width="100%" height="100%" fill="#f6f7eb"/>')
    parts.append('<text x="50%" y="40" text-anchor="middle" font-size="28" font-family="monospace" fill="#172554">')
    parts.append(svg_escape(f"Thread Scaling vs Read Ratio: Local DRAM vs CXL Node 1 ({buffer_size_gb:.0f}GB, {'random' if random_access else 'sequential'}, {duration}s)"))
    parts.append("</text>")

    for panel_index, config in enumerate(configs):
        panel_x = margin_left + panel_index * (panel_width + panel_gap)
        panel_y = margin_top

        parts.append(f'<rect x="{panel_x}" y="{panel_y}" width="{panel_width}" height="{panel_height}" rx="14" fill="#ffffff" stroke="#cbd5e1" stroke-width="2"/>')
        parts.append(
            f'<text x="{panel_x + panel_width / 2}" y="{panel_y + 28}" text-anchor="middle" font-size="22" font-family="monospace" fill="#111827">{svg_escape(config.label)}</text>'
        )
        parts.append(
            f'<text x="{panel_x + panel_width / 2}" y="{panel_y + 52}" text-anchor="middle" font-size="14" font-family="monospace" fill="#475569">cpu node {config.cpu_node}, mem node {config.mem_node}</text>'
        )

        for tick in range(0, 11):
            x_value = tick / 10
            px = x_pos(panel_x, x_value)
            parts.append(
                f'<line x1="{px:.2f}" y1="{panel_y + plot_inner_top}" x2="{px:.2f}" y2="{panel_y + panel_height - plot_inner_bottom}" stroke="#e2e8f0" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{px:.2f}" y="{panel_y + panel_height - 18}" text-anchor="middle" font-size="12" font-family="monospace" fill="#475569">{x_value:.2f}</text>'
            )

        y_ticks = 6
        for tick in range(y_ticks + 1):
            y_value = y_max * tick / y_ticks
            py = y_pos(panel_y, y_value)
            parts.append(
                f'<line x1="{panel_x + plot_inner_left}" y1="{py:.2f}" x2="{panel_x + panel_width - plot_inner_right}" y2="{py:.2f}" stroke="#e2e8f0" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{panel_x + plot_inner_left - 10}" y="{py + 4:.2f}" text-anchor="end" font-size="12" font-family="monospace" fill="#475569">{int(y_value)}</text>'
            )

        parts.append(
            f'<line x1="{panel_x + plot_inner_left}" y1="{panel_y + panel_height - plot_inner_bottom}" x2="{panel_x + panel_width - plot_inner_right}" y2="{panel_y + panel_height - plot_inner_bottom}" stroke="#334155" stroke-width="2"/>'
        )
        parts.append(
            f'<line x1="{panel_x + plot_inner_left}" y1="{panel_y + plot_inner_top}" x2="{panel_x + plot_inner_left}" y2="{panel_y + panel_height - plot_inner_bottom}" stroke="#334155" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{panel_x + panel_width / 2}" y="{panel_y + panel_height - 4}" text-anchor="middle" font-size="14" font-family="monospace" fill="#0f172a">read ratio</text>'
        )
        parts.append(
            f'<text x="{panel_x + 18}" y="{panel_y + panel_height / 2}" text-anchor="middle" font-size="14" font-family="monospace" fill="#0f172a" transform="rotate(-90 {panel_x + 18} {panel_y + panel_height / 2})">total bandwidth (MB/s)</text>'
        )

        thread_map = grouped.get(config.name, {})
        for line_index, threads in enumerate(thread_counts):
            points = thread_map.get(threads, [])
            if not points:
                continue
            color = PANEL_COLORS[line_index % len(PANEL_COLORS)]
            polyline = " ".join(
                f"{x_pos(panel_x, float(point['read_ratio'])):.2f},{y_pos(panel_y, float(point['total_bandwidth_mbps'])):.2f}"
                for point in points
            )
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{polyline}"/>'
            )
            for point in points:
                cx = x_pos(panel_x, float(point["read_ratio"]))
                cy = y_pos(panel_y, float(point["total_bandwidth_mbps"]))
                parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="4.5" fill="{color}" stroke="#ffffff" stroke-width="1.5"/>')

        legend_x = panel_x + plot_inner_left + 8
        legend_y = panel_y + plot_inner_top + 8
        for line_index, threads in enumerate(thread_counts):
            color = PANEL_COLORS[line_index % len(PANEL_COLORS)]
            y = legend_y + line_index * 22
            parts.append(f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 24}" y2="{y}" stroke="{color}" stroke-width="3"/>')
            parts.append(f'<circle cx="{legend_x + 12}" cy="{y}" r="4.5" fill="{color}" stroke="#ffffff" stroke-width="1"/>')
            parts.append(
                f'<text x="{legend_x + 34}" y="{y + 5}" font-size="13" font-family="monospace" fill="#1f2937">{threads} threads</text>'
            )

    summary_y = height - 28
    success_count = sum(1 for row in rows if row.get("status") == "success")
    total_count = len(rows)
    parts.append(
        f'<text x="{width / 2}" y="{summary_y}" text-anchor="middle" font-size="13" font-family="monospace" fill="#334155">successful runs: {success_count}/{total_count} | output: {svg_escape(str(svg_path.name))}</text>'
    )
    parts.append("</svg>")
    svg_path.write_text("".join(parts))


def print_summary(rows: Iterable[Dict[str, object]], configs: List[SweepConfig], thread_counts: List[int]) -> None:
    rows = list(rows)
    print("\nSummary")
    print("=======")
    for config in configs:
        success_rows = [row for row in rows if row.get("config") == config.name and row.get("status") == "success"]
        failed_rows = [row for row in rows if row.get("config") == config.name and row.get("status") != "success"]
        print(f"{config.label}: {len(success_rows)} success, {len(failed_rows)} failed")
        for threads in thread_counts:
            thread_rows = [row for row in success_rows if int(row["threads"]) == threads]
            if not thread_rows:
                continue
            best = max(thread_rows, key=lambda item: float(item.get("total_bandwidth_mbps", 0)))
            print(
                f"  {threads:>3} threads -> peak {float(best['total_bandwidth_mbps']):10.1f} MB/s at read_ratio={float(best['read_ratio']):.2f}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DRAM vs CXL node1 sweep and draw SVG graphs.")
    parser.add_argument("--binary", default="./double_bandwidth", help="Path to the double_bandwidth binary")
    parser.add_argument("--output-dir", default="numa_results/node0_vs_node1", help="Directory for CSV/JSON/SVG outputs")
    parser.add_argument("--buffer-size-gb", type=int, default=DEFAULT_BUFFER_SIZE_GB, help="Buffer size in GiB")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="Benchmark duration per test in seconds")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout per test in seconds")
    parser.add_argument("--thread-counts", default=",".join(str(value) for value in DEFAULT_THREAD_COUNTS), help="Comma-separated thread counts")
    parser.add_argument("--read-ratios", default=",".join(str(value) for value in DEFAULT_READ_RATIOS), help="Comma-separated read ratios")
    parser.add_argument("--cpu-node", type=int, default=0, help="CPU NUMA node used for both sweeps")
    parser.add_argument("--dram-mem-node", type=int, default=0, help="Memory node for local DRAM sweep")
    parser.add_argument("--cxl-mem-node", type=int, default=1, help="Memory node for CXL sweep")
    parser.add_argument("--sequential", action="store_true", help="Use sequential instead of random access")
    args = parser.parse_args()

    binary = Path(args.binary).resolve()
    ensure_binary_exists(binary)

    thread_counts = parse_int_list(args.thread_counts)
    read_ratios = parse_float_list(args.read_ratios)
    buffer_size_bytes = args.buffer_size_gb * 1024 ** 3
    random_access = not args.sequential

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    access_tag = "seq" if args.sequential else "random"
    prefix = f"thread_scaling_bw_node{args.cpu_node}_dram{args.dram_mem_node}_cxl{args.cxl_mem_node}_{args.buffer_size_gb}gb_{access_tag}"
    csv_path = output_dir / f"{prefix}.csv"
    json_path = output_dir / f"{prefix}.json"
    svg_path = output_dir / f"{prefix}.svg"

    configs = [
        SweepConfig("local_dram", "Local DRAM", args.cpu_node, args.dram_mem_node),
        SweepConfig("cxl_node1", "CXL Node 1", args.cpu_node, args.cxl_mem_node),
    ]

    rows: List[Dict[str, object]] = []
    total = len(configs) * len(thread_counts) * len(read_ratios)
    completed = 0

    print(f"Running sweep with buffer={format_gb(buffer_size_bytes)}, duration={args.duration}s, access={'random' if random_access else 'sequential'}")
    print(f"CPU node={args.cpu_node}, DRAM mem node={args.dram_mem_node}, CXL mem node={args.cxl_mem_node}")
    print(f"Thread counts={thread_counts}")
    print(f"Read ratios={read_ratios}")
    print(f"Total tests={total}")

    for config in configs:
        print(f"\n== {config.label} (cpu={config.cpu_node}, mem={config.mem_node}) ==")
        for threads in thread_counts:
            for read_ratio in read_ratios:
                completed += 1
                print(f"[{completed}/{total}] threads={threads:>3} read_ratio={read_ratio:.2f}")
                row = run_benchmark(
                    binary=binary,
                    config=config,
                    buffer_size_bytes=buffer_size_bytes,
                    threads=threads,
                    duration=args.duration,
                    read_ratio=read_ratio,
                    random_access=random_access,
                    timeout=args.timeout,
                )
                rows.append(row)
                if row.get("status") == "success":
                    print(
                        f"  total_bw={float(row.get('total_bandwidth_mbps', 0)):10.1f} MB/s "
                        f"read_bw={float(row.get('read_bandwidth_mbps', 0)):10.1f} "
                        f"write_bw={float(row.get('write_bandwidth_mbps', 0)):10.1f}"
                    )
                else:
                    print(f"  failed: {row.get('error', 'unknown error')}")

    write_csv(rows, csv_path)
    write_json(rows, json_path)
    make_svg(
        rows=rows,
        svg_path=svg_path,
        configs=configs,
        thread_counts=thread_counts,
        buffer_size_gb=args.buffer_size_gb,
        duration=args.duration,
        random_access=random_access,
    )
    print_summary(rows, configs, thread_counts)

    print(f"\nWrote CSV:  {csv_path}")
    print(f"Wrote JSON: {json_path}")
    print(f"Wrote SVG:  {svg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
