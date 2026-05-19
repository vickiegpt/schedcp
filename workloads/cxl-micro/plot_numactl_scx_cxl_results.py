#!/usr/bin/env python3
"""
Draw SVG comparison graphs for run_numactl_scx_cxl_bench.py results.

This script intentionally uses only the Python standard library so it works on
benchmark hosts without matplotlib or pandas installed.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Tuple


MODE_ORDER = ["numactl_local", "numactl_cxl", "numactl_local_cxl", "scx_cxl"]
MODE_LABELS = {
    "numactl_local": "Local DRAM",
    "numactl_cxl": "CXL",
    "numactl_local_cxl": "Local + CXL",
    "scx_cxl": "scx_cxl",
}
MODE_COLORS = {
    "numactl_local": "#2563eb",
    "numactl_cxl": "#dc2626",
    "numactl_local_cxl": "#16a34a",
    "scx_cxl": "#7c3aed",
}


def svg_escape(text: object) -> str:
    value = str(text)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


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


def read_rows(csv_path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") != "success":
                continue
            row["threads_requested"] = int(float(row["threads_requested"]))
            row["read_ratio_requested"] = float(row["read_ratio_requested"])
            row["total_bandwidth_mbps"] = float(row["total_bandwidth_mbps"])
            rows.append(row)
    return rows


def group_rows(rows: Iterable[Dict[str, object]]) -> Dict[Tuple[int, str], List[Dict[str, object]]]:
    grouped: Dict[Tuple[int, str], List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["threads_requested"]), str(row["mode"]))].append(row)
    for values in grouped.values():
        values.sort(key=lambda item: float(item["read_ratio_requested"]))
    return grouped


def mean_by_mode(rows: Iterable[Dict[str, object]]) -> Dict[str, float]:
    values: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        values[str(row["mode"])].append(float(row["total_bandwidth_mbps"]))
    return {mode: mean(items) for mode, items in values.items()}


def draw_multiplot(rows: List[Dict[str, object]], output_path: Path) -> None:
    grouped = group_rows(rows)
    thread_counts = sorted({int(row["threads_requested"]) for row in rows})
    max_bw = max(float(row["total_bandwidth_mbps"]) for row in rows)
    y_max = nice_ceil(max_bw * 1.08)

    width = 1600
    height = 1100
    margin_left = 90
    margin_top = 100
    margin_right = 70
    margin_bottom = 100
    panel_gap_x = 45
    panel_gap_y = 55
    cols = 2
    rows_grid = math.ceil(len(thread_counts) / cols)
    panel_width = (width - margin_left - margin_right - panel_gap_x * (cols - 1)) / cols
    panel_height = (height - margin_top - margin_bottom - panel_gap_y * (rows_grid - 1)) / rows_grid
    inner_left = 70
    inner_right = 22
    inner_top = 44
    inner_bottom = 58

    def x_pos(panel_x: float, ratio: float) -> float:
        plot_width = panel_width - inner_left - inner_right
        return panel_x + inner_left + ratio * plot_width

    def y_pos(panel_y: float, bandwidth: float) -> float:
        plot_height = panel_height - inner_top - inner_bottom
        return panel_y + panel_height - inner_bottom - (bandwidth / y_max) * plot_height

    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    parts.append('<rect width="100%" height="100%" fill="#f8fafc"/>')
    parts.append('<text x="50%" y="42" text-anchor="middle" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#111827">')
    parts.append("CXL Microbenchmark Bandwidth")
    parts.append("</text>")
    parts.append('<text x="50%" y="72" text-anchor="middle" font-family="Arial, sans-serif" font-size="16" fill="#475569">')
    parts.append("total bandwidth vs read ratio, 16 GiB random access, 10s per point")
    parts.append("</text>")

    legend_x = margin_left
    legend_y = height - 36
    for index, mode in enumerate(MODE_ORDER):
        x = legend_x + index * 250
        color = MODE_COLORS[mode]
        parts.append(f'<line x1="{x}" y1="{legend_y}" x2="{x + 36}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>')
        parts.append(f'<circle cx="{x + 18}" cy="{legend_y}" r="5" fill="{color}" stroke="#ffffff" stroke-width="1.5"/>')
        parts.append(f'<text x="{x + 48}" y="{legend_y + 5}" font-family="Arial, sans-serif" font-size="16" fill="#1f2937">{svg_escape(MODE_LABELS[mode])}</text>')

    for panel_index, threads in enumerate(thread_counts):
        col = panel_index % cols
        row = panel_index // cols
        panel_x = margin_left + col * (panel_width + panel_gap_x)
        panel_y = margin_top + row * (panel_height + panel_gap_y)

        parts.append(
            f'<rect x="{panel_x:.1f}" y="{panel_y:.1f}" width="{panel_width:.1f}" height="{panel_height:.1f}" rx="8" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{panel_x + panel_width / 2:.1f}" y="{panel_y + 28:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="20" font-weight="700" fill="#0f172a">{threads} threads</text>'
        )

        for tick in range(0, 6):
            ratio = tick / 5
            px = x_pos(panel_x, ratio)
            parts.append(
                f'<line x1="{px:.1f}" y1="{panel_y + inner_top:.1f}" x2="{px:.1f}" y2="{panel_y + panel_height - inner_bottom:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{px:.1f}" y="{panel_y + panel_height - 23:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" fill="#64748b">{ratio:.1f}</text>'
            )

        for tick in range(0, 6):
            bandwidth = y_max * tick / 5
            py = y_pos(panel_y, bandwidth)
            parts.append(
                f'<line x1="{panel_x + inner_left:.1f}" y1="{py:.1f}" x2="{panel_x + panel_width - inner_right:.1f}" y2="{py:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{panel_x + inner_left - 10:.1f}" y="{py + 4:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="12" fill="#64748b">{bandwidth / 1000:.0f}k</text>'
            )

        axis_left = panel_x + inner_left
        axis_bottom = panel_y + panel_height - inner_bottom
        parts.append(
            f'<line x1="{axis_left:.1f}" y1="{panel_y + inner_top:.1f}" x2="{axis_left:.1f}" y2="{axis_bottom:.1f}" stroke="#334155" stroke-width="1.5"/>'
        )
        parts.append(
            f'<line x1="{axis_left:.1f}" y1="{axis_bottom:.1f}" x2="{panel_x + panel_width - inner_right:.1f}" y2="{axis_bottom:.1f}" stroke="#334155" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{panel_x + panel_width / 2:.1f}" y="{panel_y + panel_height - 6:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#334155">read ratio</text>'
        )
        parts.append(
            f'<text x="{panel_x + 20:.1f}" y="{panel_y + panel_height / 2:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#334155" transform="rotate(-90 {panel_x + 20:.1f} {panel_y + panel_height / 2:.1f})">MB/s</text>'
        )

        for mode in MODE_ORDER:
            points = grouped.get((threads, mode), [])
            if not points:
                continue
            color = MODE_COLORS[mode]
            polyline = " ".join(
                f"{x_pos(panel_x, float(point['read_ratio_requested'])):.1f},{y_pos(panel_y, float(point['total_bandwidth_mbps'])):.1f}"
                for point in points
            )
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{polyline}"/>')
            for point in points:
                cx = x_pos(panel_x, float(point["read_ratio_requested"]))
                cy = y_pos(panel_y, float(point["total_bandwidth_mbps"]))
                parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}" stroke="#ffffff" stroke-width="1.2"/>')

    means = mean_by_mode(rows)
    summary = " | ".join(f"{MODE_LABELS[mode]} mean {means[mode] / 1000:.1f} GB/s" for mode in MODE_ORDER if mode in means)
    parts.append(f'<text x="50%" y="{height - 10}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#475569">{svg_escape(summary)}</text>')
    parts.append("</svg>")
    output_path.write_text("".join(parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw SVG graphs for numactl/scx_cxl CXL microbenchmark results.")
    parser.add_argument("csv", nargs="?", default="workloads/cxl-micro/results/numactl_scx_cxl/full_20260519_0515/results.csv", help="Path to results.csv")
    parser.add_argument("--output", default="", help="Output SVG path")
    args = parser.parse_args()

    csv_path = Path(args.csv).resolve()
    output_path = Path(args.output).resolve() if args.output else csv_path.with_name("bandwidth_by_read_ratio.svg")

    rows = read_rows(csv_path)
    if not rows:
        raise SystemExit(f"No successful rows found in {csv_path}")
    draw_multiplot(rows, output_path)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
