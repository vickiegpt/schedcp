#!/usr/bin/env python3
"""
Benchmark CXL memory policy modes with double_bandwidth.

The modes are:
  default  - kernel NUMA balancing disabled
  autonuma - kernel NUMA balancing enabled
  tpp      - Linux tiered page placement candidate, using numa_balancing=2
             plus demotion_enabled=true when that sysfs knob exists

The runner preserves the original kernel knobs and restores them before exit.
It also samples /proc/<pid>/numa_maps while each benchmark is running so the
report can show whether pages actually reached the CXL/slow-memory node.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
NUMA_BALANCING = Path("/proc/sys/kernel/numa_balancing")
PROMOTE_RATE = Path("/proc/sys/kernel/numa_balancing_promote_rate_limit_MBps")
DEMOTION_ENABLED = Path("/sys/kernel/mm/numa/demotion_enabled")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

DEFAULT_POLICIES = ["default", "autonuma", "tpp"]
DEFAULT_THREAD_COUNTS = [16, 48]
DEFAULT_READ_RATIOS = [0.0, 0.5, 1.0]
DEFAULT_ACCESS_PATTERNS = ["random"]


@dataclass(frozen=True)
class Policy:
    name: str
    label: str
    numa_balancing: str
    demotion_enabled: Optional[str]


@dataclass(frozen=True)
class NodeTopology:
    local_nodes: List[int]
    cxl_nodes: List[int]
    memory_nodes: List[int]
    cpu_nodes: List[int]
    real_cxl: bool
    raw_numactl: str

    @property
    def local_nodes_csv(self) -> str:
        return ",".join(str(node) for node in self.local_nodes)

    @property
    def cxl_nodes_csv(self) -> str:
        return ",".join(str(node) for node in self.cxl_nodes)

    @property
    def local_cxl_nodes_csv(self) -> str:
        return ",".join(str(node) for node in sorted(set(self.local_nodes + self.cxl_nodes)))


POLICIES: Dict[str, Policy] = {
    "default": Policy("default", "Default, NUMA balancing off", "0", "false"),
    "autonuma": Policy("autonuma", "AutoNUMA", "1", "false"),
    "tpp": Policy("tpp", "TPP / memory tiering", "2", "true"),
}

POLICY_COLORS = {
    "default": "#2563eb",
    "autonuma": "#16a34a",
    "tpp": "#dc2626",
}


def parse_int_list(value: str) -> List[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def parse_float_list(value: str) -> List[float]:
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def parse_csv_list(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def run_text_command(cmd: Sequence[str], timeout: int = 10) -> str:
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{cmd[0]} failed")
    return completed.stdout


def detect_topology(local_override: str = "", cxl_override: str = "") -> NodeTopology:
    try:
        output = run_text_command(["numactl", "--hardware"])
    except Exception as exc:
        output = f"numactl --hardware failed: {exc}"
        local_nodes = parse_int_list(local_override) if local_override else [0]
        cxl_nodes = parse_int_list(cxl_override) if cxl_override else []
        return NodeTopology(local_nodes, cxl_nodes, local_nodes + cxl_nodes, local_nodes, bool(cxl_nodes), output)

    nodes: Dict[int, Dict[str, object]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("node "):
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        try:
            node_id = int(parts[1])
        except ValueError:
            continue

        info = nodes.setdefault(node_id, {"cpus": [], "size_mb": 0})
        if parts[2] == "cpus:":
            info["cpus"] = parts[3:]
        elif parts[2] == "size:":
            try:
                info["size_mb"] = int(parts[3])
            except ValueError:
                pass

    memory_nodes = [node for node, info in sorted(nodes.items()) if int(info.get("size_mb", 0)) > 0]
    cpu_nodes = [node for node, info in sorted(nodes.items()) if info.get("cpus")]
    memory_only_nodes = [node for node in memory_nodes if node not in cpu_nodes]

    local_nodes = parse_int_list(local_override) if local_override else (cpu_nodes or memory_nodes[:1] or [0])
    if cxl_override:
        cxl_nodes = parse_int_list(cxl_override)
    else:
        cxl_nodes = memory_only_nodes or [node for node in memory_nodes if node not in local_nodes]

    real_cxl = bool(cxl_nodes) and bool(set(cxl_nodes) - set(local_nodes))
    return NodeTopology(local_nodes, cxl_nodes, memory_nodes, cpu_nodes, real_cxl, output)


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def write_text(path: Path, value: str) -> None:
    path.write_text(value + "\n")


def snapshot_kernel_knobs() -> Dict[str, Optional[str]]:
    return {
        str(NUMA_BALANCING): read_text(NUMA_BALANCING),
        str(PROMOTE_RATE): read_text(PROMOTE_RATE),
        str(DEMOTION_ENABLED): read_text(DEMOTION_ENABLED),
    }


def restore_kernel_knobs(snapshot: Dict[str, Optional[str]]) -> List[str]:
    errors: List[str] = []
    for raw_path, value in snapshot.items():
        if value is None:
            continue
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            write_text(path, value)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors


def apply_policy(policy: Policy, promote_rate_mbps: int, settle_s: float) -> Tuple[bool, Dict[str, Optional[str]], str]:
    errors: List[str] = []
    try:
        write_text(NUMA_BALANCING, policy.numa_balancing)
    except OSError as exc:
        errors.append(f"{NUMA_BALANCING}: {exc}")

    if policy.demotion_enabled is not None and DEMOTION_ENABLED.exists():
        try:
            write_text(DEMOTION_ENABLED, policy.demotion_enabled)
        except OSError as exc:
            errors.append(f"{DEMOTION_ENABLED}: {exc}")

    if policy.name == "tpp" and promote_rate_mbps > 0 and PROMOTE_RATE.exists():
        try:
            write_text(PROMOTE_RATE, str(promote_rate_mbps))
        except OSError as exc:
            errors.append(f"{PROMOTE_RATE}: {exc}")

    if settle_s > 0:
        time.sleep(settle_s)

    snapshot = snapshot_kernel_knobs()
    return not errors, snapshot, "; ".join(errors)


def ensure_executable(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"{label} is not executable: {path}")


def make_workload_command(
    args: argparse.Namespace,
    threads: int,
    read_ratio: float,
    access_pattern: str,
) -> List[str]:
    binary = Path(args.binary).resolve()
    cmd = [
        str(binary),
        "--buffer-size",
        str(int(args.buffer_size_gb * 1024**3)),
        "--threads",
        str(threads),
        "--duration",
        str(args.duration),
        "--read-ratio",
        str(read_ratio),
        "--json",
        "--no-numa",
    ]
    cmd.append("--random" if access_pattern == "random" else "--sequential")
    return cmd


def wrap_with_numactl(cmd: Sequence[str], args: argparse.Namespace, topology: NodeTopology) -> List[str]:
    wrapped = ["numactl", f"--cpunodebind={args.cpu_node}"]

    if args.placement == "kernel":
        return [*wrapped, *cmd]
    if args.placement == "local":
        return [*wrapped, f"--membind={topology.local_nodes_csv}", *cmd]
    if args.placement == "cxl":
        return [*wrapped, f"--membind={topology.cxl_nodes_csv}", *cmd]
    if args.placement == "interleave":
        return [*wrapped, f"--interleave={topology.local_cxl_nodes_csv}", *cmd]
    raise ValueError(f"unknown placement: {args.placement}")


def truncate_text(value: str, limit: int = 4096) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} bytes ..."


def sample_numa_maps(pid: int, started_at: float) -> Optional[Dict[str, object]]:
    path = Path(f"/proc/{pid}/numa_maps")
    try:
        text = path.read_text()
    except OSError:
        return None

    node_pages: Dict[str, int] = {}
    for line in text.splitlines():
        for match in re.finditer(r"\bN(\d+)=(\d+)\b", line):
            node = match.group(1)
            pages = int(match.group(2))
            node_pages[node] = node_pages.get(node, 0) + pages

    node_mb = {
        node: pages * PAGE_SIZE / 1024 / 1024
        for node, pages in sorted(node_pages.items(), key=lambda item: int(item[0]))
    }
    return {
        "t_s": time.time() - started_at,
        "node_pages": node_pages,
        "node_mb": node_mb,
        "total_mb": sum(node_mb.values()),
    }


def peak_node_mb(samples: Iterable[Dict[str, object]]) -> Dict[str, float]:
    peaks: Dict[str, float] = {}
    for sample in samples:
        node_mb = sample.get("node_mb", {})
        if not isinstance(node_mb, dict):
            continue
        for node, value in node_mb.items():
            peaks[str(node)] = max(peaks.get(str(node), 0.0), float(value))
    return peaks


def sum_nodes(node_mb: Dict[str, float], nodes: Sequence[int]) -> float:
    return sum(float(node_mb.get(str(node), 0.0)) for node in nodes)


def run_one(command: Sequence[str], timeout: int, sample_interval_s: float) -> Dict[str, object]:
    started_at = time.time()
    samples: List[Dict[str, object]] = []
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    timed_out = False
    while proc.poll() is None:
        sample = sample_numa_maps(proc.pid, started_at)
        if sample is not None:
            samples.append(sample)

        if time.time() - started_at > timeout:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            break

        time.sleep(sample_interval_s)

    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        timed_out = True

    wall_clock_s = time.time() - started_at
    row: Dict[str, object] = {
        "returncode": proc.returncode,
        "wall_clock_s": wall_clock_s,
        "numa_samples": samples,
        "sample_count": len(samples),
    }

    if timed_out:
        row.update(
            {
                "status": "timeout",
                "error": f"timeout after {timeout}s",
                "stdout": truncate_text(stdout),
                "stderr": truncate_text(stderr),
            }
        )
        return row

    if proc.returncode != 0:
        row.update(
            {
                "status": "failed",
                "error": truncate_text(stderr.strip() or stdout.strip() or "benchmark failed"),
                "stdout": truncate_text(stdout),
                "stderr": truncate_text(stderr),
            }
        )
        return row

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        row.update(
            {
                "status": "failed",
                "error": f"failed to parse JSON output: {exc}",
                "stdout": truncate_text(stdout),
                "stderr": truncate_text(stderr),
            }
        )
        return row

    row["status"] = "success"
    row.update(parsed)
    return row


def write_json(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    with path.open("w") as handle:
        json.dump(list(rows), handle, indent=2, sort_keys=True)


def flatten_for_csv(row: Dict[str, object]) -> Dict[str, object]:
    flattened = dict(row)
    flattened.pop("numa_samples", None)
    return flattened


def write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    flattened = [flatten_for_csv(row) for row in rows]
    if not flattened:
        return
    fieldnames = sorted({key for row in flattened for key in row.keys()})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened)


def append_jsonl(path: Path, row: Dict[str, object]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_numa_samples(path: Path, run_id: str, samples: Iterable[Dict[str, object]]) -> None:
    with path.open("a") as handle:
        for sample in samples:
            payload = {"run_id": run_id, **sample}
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def svg_escape(value: object) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
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


def draw_svg(rows: List[Dict[str, object]], output_path: Path) -> None:
    successes = [row for row in rows if row.get("status") == "success"]
    if not successes:
        return

    panels = sorted({(int(row["threads_requested"]), str(row["access_pattern_requested"])) for row in successes})
    max_bw = max(float(row.get("total_bandwidth_mbps", 0.0)) for row in successes)
    y_max = nice_ceil(max_bw * 1.08)

    width = 1400
    cols = 2
    rows_grid = max(1, math.ceil(len(panels) / cols))
    panel_h = 330
    height = 160 + rows_grid * panel_h + 80
    margin_left = 90
    margin_top = 110
    panel_gap_x = 45
    panel_gap_y = 45
    panel_width = (width - margin_left - 70 - panel_gap_x * (cols - 1)) / cols
    inner_left = 70
    inner_right = 26
    inner_top = 44
    inner_bottom = 58

    def x_pos(panel_x: float, ratio: float) -> float:
        return panel_x + inner_left + ratio * (panel_width - inner_left - inner_right)

    def y_pos(panel_y: float, bandwidth: float) -> float:
        plot_height = panel_h - inner_top - inner_bottom
        return panel_y + panel_h - inner_bottom - (bandwidth / y_max) * plot_height

    grouped: Dict[Tuple[int, str, str], List[Dict[str, object]]] = {}
    for row in successes:
        key = (int(row["threads_requested"]), str(row["access_pattern_requested"]), str(row["policy"]))
        grouped.setdefault(key, []).append(row)
    for values in grouped.values():
        values.sort(key=lambda item: float(item["read_ratio_requested"]))

    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    parts.append('<rect width="100%" height="100%" fill="#f8fafc"/>')
    parts.append('<text x="50%" y="42" text-anchor="middle" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#111827">CXL Policy Bandwidth</text>')
    parts.append('<text x="50%" y="72" text-anchor="middle" font-family="Arial, sans-serif" font-size="16" fill="#475569">double_bandwidth across default, AutoNUMA, and TPP modes</text>')

    legend_y = 102
    legend_x = margin_left
    for index, policy in enumerate(DEFAULT_POLICIES):
        x = legend_x + index * 210
        color = POLICY_COLORS[policy]
        parts.append(f'<line x1="{x}" y1="{legend_y}" x2="{x + 34}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>')
        parts.append(f'<circle cx="{x + 17}" cy="{legend_y}" r="5" fill="{color}" stroke="#ffffff" stroke-width="1.3"/>')
        parts.append(f'<text x="{x + 46}" y="{legend_y + 5}" font-family="Arial, sans-serif" font-size="15" fill="#1f2937">{svg_escape(POLICIES[policy].label)}</text>')

    for panel_index, (threads, access_pattern) in enumerate(panels):
        col = panel_index % cols
        row_index = panel_index // cols
        panel_x = margin_left + col * (panel_width + panel_gap_x)
        panel_y = margin_top + row_index * (panel_h + panel_gap_y)

        parts.append(f'<rect x="{panel_x:.1f}" y="{panel_y:.1f}" width="{panel_width:.1f}" height="{panel_h:.1f}" rx="8" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.3"/>')
        parts.append(f'<text x="{panel_x + panel_width / 2:.1f}" y="{panel_y + 28:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="19" font-weight="700" fill="#0f172a">{threads} threads, {svg_escape(access_pattern)}</text>')

        for tick in range(6):
            ratio = tick / 5
            px = x_pos(panel_x, ratio)
            parts.append(f'<line x1="{px:.1f}" y1="{panel_y + inner_top:.1f}" x2="{px:.1f}" y2="{panel_y + panel_h - inner_bottom:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
            parts.append(f'<text x="{px:.1f}" y="{panel_y + panel_h - 24:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" fill="#64748b">{ratio:.1f}</text>')

        for tick in range(6):
            bandwidth = y_max * tick / 5
            py = y_pos(panel_y, bandwidth)
            parts.append(f'<line x1="{panel_x + inner_left:.1f}" y1="{py:.1f}" x2="{panel_x + panel_width - inner_right:.1f}" y2="{py:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
            parts.append(f'<text x="{panel_x + inner_left - 10:.1f}" y="{py + 4:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="12" fill="#64748b">{bandwidth / 1000:.0f}k</text>')

        axis_left = panel_x + inner_left
        axis_bottom = panel_y + panel_h - inner_bottom
        parts.append(f'<line x1="{axis_left:.1f}" y1="{panel_y + inner_top:.1f}" x2="{axis_left:.1f}" y2="{axis_bottom:.1f}" stroke="#334155" stroke-width="1.4"/>')
        parts.append(f'<line x1="{axis_left:.1f}" y1="{axis_bottom:.1f}" x2="{panel_x + panel_width - inner_right:.1f}" y2="{axis_bottom:.1f}" stroke="#334155" stroke-width="1.4"/>')
        parts.append(f'<text x="{panel_x + panel_width / 2:.1f}" y="{panel_y + panel_h - 7:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#334155">read ratio</text>')
        parts.append(f'<text x="{panel_x + 20:.1f}" y="{panel_y + panel_h / 2:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#334155" transform="rotate(-90 {panel_x + 20:.1f} {panel_y + panel_h / 2:.1f})">MB/s</text>')

        for policy in DEFAULT_POLICIES:
            points = grouped.get((threads, access_pattern, policy), [])
            if not points:
                continue
            color = POLICY_COLORS[policy]
            polyline = " ".join(
                f"{x_pos(panel_x, float(point['read_ratio_requested'])):.1f},{y_pos(panel_y, float(point.get('total_bandwidth_mbps', 0.0))):.1f}"
                for point in points
            )
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{polyline}"/>')
            for point in points:
                cx = x_pos(panel_x, float(point["read_ratio_requested"]))
                cy = y_pos(panel_y, float(point.get("total_bandwidth_mbps", 0.0)))
                parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{color}" stroke="#ffffff" stroke-width="1.2"/>')

    means = {}
    for policy in DEFAULT_POLICIES:
        vals = [float(row.get("total_bandwidth_mbps", 0.0)) for row in successes if row.get("policy") == policy]
        if vals:
            means[policy] = mean(vals)
    summary = " | ".join(f"{POLICIES[policy].label}: {means[policy] / 1000:.1f} GB/s mean" for policy in DEFAULT_POLICIES if policy in means)
    parts.append(f'<text x="50%" y="{height - 30}" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#475569">{svg_escape(summary)}</text>')
    parts.append("</svg>")
    output_path.write_text("".join(parts))


def draw_pdf(rows: List[Dict[str, object]], output_path: Path) -> bool:
    successes = [row for row in rows if row.get("status") == "success"]
    if not successes:
        return False

    try:
        os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False

    panels = sorted({(int(row["threads_requested"]), str(row["access_pattern_requested"])) for row in successes})
    cols = 2
    rows_grid = max(1, math.ceil(len(panels) / cols))
    fig, axes = plt.subplots(rows_grid, cols, figsize=(14, max(4.5, rows_grid * 4.2)), squeeze=False)

    grouped: Dict[Tuple[int, str, str], List[Dict[str, object]]] = {}
    for row in successes:
        key = (int(row["threads_requested"]), str(row["access_pattern_requested"]), str(row["policy"]))
        grouped.setdefault(key, []).append(row)
    for values in grouped.values():
        values.sort(key=lambda item: float(item["read_ratio_requested"]))

    for panel_index, (threads, access_pattern) in enumerate(panels):
        ax = axes[panel_index // cols][panel_index % cols]
        for policy in DEFAULT_POLICIES:
            points = grouped.get((threads, access_pattern, policy), [])
            if not points:
                continue
            xs = [float(point["read_ratio_requested"]) for point in points]
            ys = [float(point.get("total_bandwidth_mbps", 0.0)) / 1000.0 for point in points]
            ax.plot(xs, ys, marker="o", linewidth=2.2, label=POLICIES[policy].label, color=POLICY_COLORS[policy])
        ax.set_title(f"{threads} threads, {access_pattern}")
        ax.set_xlabel("read ratio")
        ax.set_ylabel("bandwidth (GB/s)")
        ax.grid(True, color="#e5e7eb")
        ax.set_xlim(-0.02, 1.02)

    for panel_index in range(len(panels), rows_grid * cols):
        axes[panel_index // cols][panel_index % cols].axis("off")

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=min(3, len(handles)), frameon=False)
    fig.suptitle("CXL Policy Bandwidth", fontsize=16, y=0.98)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(output_path)
    plt.close(fig)
    return True


def write_report(
    path: Path,
    rows: List[Dict[str, object]],
    topology: NodeTopology,
    args: argparse.Namespace,
    kernel_before: Dict[str, Optional[str]],
    restore_errors: List[str],
    pdf_written: bool,
) -> None:
    successes = [row for row in rows if row.get("status") == "success"]
    lines: List[str] = []
    lines.append("# CXL TPP and AutoNUMA Benchmark")
    lines.append("")
    lines.append(f"- real CXL node visible: `{topology.real_cxl}`")
    lines.append(f"- local nodes: `{topology.local_nodes_csv}`")
    lines.append(f"- CXL/slow-memory nodes: `{topology.cxl_nodes_csv or 'none'}`")
    lines.append(f"- placement policy: `{args.placement}`")
    lines.append(f"- buffer size: `{args.buffer_size_gb:g} GiB`")
    lines.append(f"- duration per point: `{args.duration}s`")
    lines.append(f"- kernel knobs before run: `{json.dumps(kernel_before, sort_keys=True)}`")
    lines.append("")
    if not topology.real_cxl:
        lines.append("**CXL warning:** this boot exposes only local memory, so these numbers are smoke-test data rather than real CXL/TPP results.")
        lines.append("")
    if restore_errors:
        lines.append("**Restore warning:** some kernel knobs failed to restore:")
        for error in restore_errors:
            lines.append(f"- `{error}`")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| policy | successful points | mean bandwidth GB/s | best bandwidth GB/s | peak local MB | peak CXL MB |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for policy in DEFAULT_POLICIES:
        policy_rows = [row for row in successes if row.get("policy") == policy]
        if not policy_rows:
            lines.append(f"| {policy} | 0 | n/a | n/a | n/a | n/a |")
            continue
        bandwidths = [float(row.get("total_bandwidth_mbps", 0.0)) for row in policy_rows]
        best = max(bandwidths)
        peak_local = max(float(row.get("peak_local_mb", 0.0)) for row in policy_rows)
        peak_cxl = max(float(row.get("peak_cxl_mb", 0.0)) for row in policy_rows)
        lines.append(f"| {policy} | {len(policy_rows)} | {mean(bandwidths) / 1000:.3f} | {best / 1000:.3f} | {peak_local:.1f} | {peak_cxl:.1f} |")

    failures = [row for row in rows if row.get("status") != "success"]
    if failures:
        lines.append("")
        lines.append("## Failures")
        lines.append("")
        for row in failures:
            lines.append(f"- `{row.get('run_id')}`: `{row.get('status')}` `{row.get('error', '')}`")

    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `results.csv`")
    lines.append("- `results.json`")
    lines.append("- `results.jsonl`")
    lines.append("- `numa_maps_samples.jsonl`")
    lines.append("- `bandwidth_by_policy.svg`")
    if pdf_written:
        lines.append("- `bandwidth_by_policy.pdf`")
    path.write_text("\n".join(lines) + "\n")


def print_summary(rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    print("\nSummary")
    print("=======")
    for policy in DEFAULT_POLICIES:
        policy_rows = [row for row in rows if row.get("policy") == policy]
        successes = [row for row in policy_rows if row.get("status") == "success"]
        if not policy_rows:
            continue
        if not successes:
            print(f"{policy}: 0/{len(policy_rows)} success")
            continue
        bandwidths = [float(row.get("total_bandwidth_mbps", 0.0)) for row in successes]
        peak_cxl = max(float(row.get("peak_cxl_mb", 0.0)) for row in successes)
        print(f"{policy}: {len(successes)}/{len(policy_rows)} success, mean={mean(bandwidths):.1f} MB/s, peak_cxl={peak_cxl:.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CXL double_bandwidth under default, AutoNUMA, and TPP kernel policies.")
    parser.add_argument("--binary", default=str(SCRIPT_DIR / "double_bandwidth"), help="Path to double_bandwidth")
    parser.add_argument("--output-dir", default="", help="Output directory; defaults to timestamped results dir")
    parser.add_argument("--policies", default=",".join(DEFAULT_POLICIES), help=f"Comma-separated policies: {', '.join(DEFAULT_POLICIES)}")
    parser.add_argument("--thread-counts", default=",".join(str(value) for value in DEFAULT_THREAD_COUNTS), help="Comma-separated thread counts")
    parser.add_argument("--read-ratios", default=",".join(str(value) for value in DEFAULT_READ_RATIOS), help="Comma-separated read ratios")
    parser.add_argument("--access-patterns", default=",".join(DEFAULT_ACCESS_PATTERNS), help="Comma-separated access patterns: random,sequential")
    parser.add_argument("--buffer-size-gb", type=float, default=16.0, help="Buffer size per run in GiB")
    parser.add_argument("--duration", type=int, default=10, help="Duration per run in seconds")
    parser.add_argument("--timeout", type=int, default=0, help="Timeout per run in seconds; default duration + 120")
    parser.add_argument("--cpu-node", type=int, default=0, help="CPU NUMA node for numactl --cpunodebind")
    parser.add_argument("--local-nodes", default="", help="Override local DRAM nodes")
    parser.add_argument("--cxl-nodes", default="", help="Override CXL/slow-memory nodes")
    parser.add_argument("--placement", choices=["kernel", "local", "cxl", "interleave"], default="kernel", help="Memory policy used around the benchmark")
    parser.add_argument("--promote-rate-mbps", type=int, default=0, help="Optional TPP promote rate limit written to numa_balancing_promote_rate_limit_MBps")
    parser.add_argument("--settle-s", type=float, default=1.0, help="Seconds to wait after changing kernel knobs")
    parser.add_argument("--numa-map-interval-s", type=float, default=0.5, help="Sampling interval for /proc/<pid>/numa_maps")
    parser.add_argument("--allow-single-node-smoke", action="store_true", help="Run even when no distinct CXL/slow-memory node is visible")
    args = parser.parse_args()

    binary = Path(args.binary).resolve()
    ensure_executable(binary, "double_bandwidth")

    requested_policies = parse_csv_list(args.policies)
    unknown = [policy for policy in requested_policies if policy not in POLICIES]
    if unknown:
        raise ValueError(f"unknown policy/policies: {', '.join(unknown)}")

    access_patterns = parse_csv_list(args.access_patterns)
    invalid_patterns = [pattern for pattern in access_patterns if pattern not in {"random", "sequential"}]
    if invalid_patterns:
        raise ValueError(f"invalid access pattern(s): {', '.join(invalid_patterns)}")

    topology = detect_topology(args.local_nodes, args.cxl_nodes)
    if not topology.real_cxl and not args.allow_single_node_smoke:
        print("No distinct CXL/slow-memory NUMA node is visible.", file=sys.stderr)
        print("Rerun on a boot with CXL memory exposed, pass --cxl-nodes, or use --allow-single-node-smoke for a local-only smoke run.", file=sys.stderr)
        return 2

    if args.placement in {"cxl", "interleave"} and not topology.cxl_nodes:
        print(f"Placement {args.placement} requires CXL nodes; use --cxl-nodes or --allow-single-node-smoke with placement=kernel/local.", file=sys.stderr)
        return 2

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = SCRIPT_DIR / "results" / "cxl_tpp_autonuma" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    thread_counts = parse_int_list(args.thread_counts)
    read_ratios = parse_float_list(args.read_ratios)
    timeout = args.timeout if args.timeout > 0 else args.duration + 120
    kernel_before = snapshot_kernel_knobs()

    csv_path = output_dir / "results.csv"
    json_path = output_dir / "results.json"
    jsonl_path = output_dir / "results.jsonl"
    samples_path = output_dir / "numa_maps_samples.jsonl"
    env_path = output_dir / "environment.json"
    graph_path = output_dir / "bandwidth_by_policy.svg"
    pdf_path = output_dir / "bandwidth_by_policy.pdf"
    report_path = output_dir / "report.md"

    environment = {
        "topology": {
            "local_nodes": topology.local_nodes,
            "cxl_nodes": topology.cxl_nodes,
            "memory_nodes": topology.memory_nodes,
            "cpu_nodes": topology.cpu_nodes,
            "real_cxl": topology.real_cxl,
            "numactl_hardware": topology.raw_numactl,
        },
        "kernel_before": kernel_before,
        "args": vars(args),
    }
    env_path.write_text(json.dumps(environment, indent=2, sort_keys=True) + "\n")

    print(f"Output directory: {output_dir}")
    print(f"Real CXL visible: {topology.real_cxl}")
    print(f"Local nodes: {topology.local_nodes_csv}")
    print(f"CXL nodes: {topology.cxl_nodes_csv or 'none'}")
    print(f"Policies: {requested_policies}")
    print(f"Placement: {args.placement}")

    rows: List[Dict[str, object]] = []
    total_runs = len(requested_policies) * len(thread_counts) * len(read_ratios) * len(access_patterns)
    run_index = 0
    restore_errors: List[str] = []

    try:
        for policy_name in requested_policies:
            policy = POLICIES[policy_name]
            setup_ok, kernel_after_setup, setup_error = apply_policy(policy, args.promote_rate_mbps, args.settle_s)

            for access_pattern in access_patterns:
                for threads in thread_counts:
                    for read_ratio in read_ratios:
                        run_index += 1
                        run_id = f"{run_index:04d}_{policy_name}_{access_pattern}_t{threads}_r{read_ratio:.2f}"
                        print(f"[{run_index}/{total_runs}] policy={policy_name} pattern={access_pattern} threads={threads} read_ratio={read_ratio:.2f}")

                        command = wrap_with_numactl(make_workload_command(args, threads, read_ratio, access_pattern), args, topology)
                        if setup_ok:
                            row = run_one(command, timeout, args.numa_map_interval_s)
                        else:
                            row = {
                                "status": "setup_failed",
                                "returncode": -1,
                                "error": setup_error,
                                "numa_samples": [],
                                "sample_count": 0,
                            }

                        samples = row.get("numa_samples", [])
                        sample_list = samples if isinstance(samples, list) else []
                        peaks = peak_node_mb(sample_list)
                        row.update(
                            {
                                "run_id": run_id,
                                "policy": policy_name,
                                "policy_label": policy.label,
                                "threads_requested": threads,
                                "read_ratio_requested": read_ratio,
                                "access_pattern_requested": access_pattern,
                                "buffer_size_gb_requested": args.buffer_size_gb,
                                "duration_requested": args.duration,
                                "placement": args.placement,
                                "cpu_node": args.cpu_node,
                                "local_nodes": topology.local_nodes_csv,
                                "cxl_nodes": topology.cxl_nodes_csv,
                                "real_cxl_visible": topology.real_cxl,
                                "kernel_numa_balancing": kernel_after_setup.get(str(NUMA_BALANCING)),
                                "kernel_demotion_enabled": kernel_after_setup.get(str(DEMOTION_ENABLED)),
                                "kernel_promote_rate_mbps": kernel_after_setup.get(str(PROMOTE_RATE)),
                                "peak_local_mb": sum_nodes(peaks, topology.local_nodes),
                                "peak_cxl_mb": sum_nodes(peaks, topology.cxl_nodes),
                                "peak_total_mb": sum(peaks.values()),
                                "command": " ".join(shlex.quote(part) for part in command),
                            }
                        )

                        write_numa_samples(samples_path, run_id, sample_list)
                        rows.append(row)
                        append_jsonl(jsonl_path, row)
                        write_json(json_path, rows)
                        write_csv(csv_path, rows)

                        if row.get("status") == "success":
                            print(
                                f"  total_bw={float(row.get('total_bandwidth_mbps', 0.0)):10.1f} MB/s "
                                f"peak_cxl={float(row.get('peak_cxl_mb', 0.0)):8.1f} MB"
                            )
                        else:
                            print(f"  {row.get('status')}: {row.get('error', 'unknown error')}")
    finally:
        restore_errors = restore_kernel_knobs(kernel_before)

    draw_svg(rows, graph_path)
    pdf_written = draw_pdf(rows, pdf_path)
    write_report(report_path, rows, topology, args, kernel_before, restore_errors, pdf_written)

    print_summary(rows)
    print(f"\nWrote CSV:     {csv_path}")
    print(f"Wrote JSON:    {json_path}")
    print(f"Wrote samples: {samples_path}")
    print(f"Wrote graph:   {graph_path}")
    if pdf_written:
        print(f"Wrote PDF:     {pdf_path}")
    print(f"Wrote report:  {report_path}")
    if restore_errors:
        print("Restore errors:", file=sys.stderr)
        for error in restore_errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)
