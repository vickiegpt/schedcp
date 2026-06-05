#!/usr/bin/env python3
"""Scheduler benchmark harness for memory-bandwidth-bound HPC proxy kernels."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.append(str(repo_root / "scheduler"))

from scheduler_runner import SchedulerRunner


class HpcMemoryBenchmark:
    def __init__(self, binary=None, results_dir="results", scheduler_runner=None):
        self.workload_dir = Path(__file__).resolve().parent
        self.binary = str(binary or self.workload_dir / "hpc_memory_kernels")
        self.results_dir = Path(results_dir)
        self.runner = scheduler_runner or SchedulerRunner()
        self.results_dir.mkdir(parents=True, exist_ok=True)

        if not os.path.exists(self.binary):
            raise FileNotFoundError(
                f"{self.binary} not found; run 'make -C workloads/hpc-memory build' first"
            )

    def run_once(self, scheduler_name=None, kernel="all", threads=0,
                 size_mib=256, iterations=3, slug_hint="pipeline", timeout=900):
        cmd = [
            self.binary,
            f"--kernel={kernel}",
            f"--threads={threads}",
            f"--size-mib={size_mib}",
            f"--iterations={iterations}",
            f"--slug-hint={slug_hint}",
        ]
        start = time.time()

        if scheduler_name:
            exit_code, stdout, stderr = self.runner.run_command_with_scheduler(
                scheduler_name, cmd, timeout=timeout
            )
        else:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired:
                exit_code, stdout, stderr = -1, "", "Command timed out"

        parsed = {}
        if exit_code == 0:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError as e:
                exit_code = -1
                stderr = f"Failed to parse JSON: {e}\n{stderr}"

        return {
            "scheduler": scheduler_name or "default",
            "command": " ".join(cmd),
            "return_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration": time.time() - start,
            "timestamp": datetime.now().isoformat(),
            "hpc_memory": parsed,
        }

    def run_suite(self, schedulers=None, include_default=True, **kwargs):
        schedulers = schedulers or []
        results = {}

        if include_default:
            print("\n=== HPC memory scheduler: default ===")
            results["default"] = self.run_once(None, **kwargs)
            self.save_results(results)

        for scheduler_name in schedulers:
            print(f"\n=== HPC memory scheduler: {scheduler_name} ===")
            results[scheduler_name] = self.run_once(scheduler_name, **kwargs)
            self.save_results(results)
            time.sleep(1)

        return results

    def save_results(self, results):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.results_dir / f"hpc_memory_benchmark_{timestamp}.json"
        with open(path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {path}")
        return path

    def generate_figure(self, results_by_scheduler):
        try:
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as e:
            print(f"Could not generate figures: {e}")
            return None

        rows = []
        for scheduler_name, result in results_by_scheduler.items():
            if result.get("return_code") != 0:
                continue
            for entry in result.get("hpc_memory", {}).get("results", []):
                rows.append({
                    "scheduler": scheduler_name,
                    "kernel": entry.get("kernel"),
                    "bandwidth": float(entry.get("bandwidth_gb_s", 0.0)),
                    "seconds": float(entry.get("seconds", 0.0)),
                })

        if not rows:
            print("No successful HPC memory results to plot")
            return None

        kernel_order = ["stream_triad", "wrf_stencil", "gromacs_pairlist", "sst_sparse", "quantum_state"]
        kernels = [k for k in kernel_order if any(row["kernel"] == k for row in rows)]
        schedulers = list(dict.fromkeys(row["scheduler"] for row in rows))
        x = np.arange(len(kernels))
        width = min(0.8 / max(1, len(schedulers)), 0.22)

        fig, ax = plt.subplots(figsize=(12, 5))
        for idx, scheduler_name in enumerate(schedulers):
            offsets = x + (idx - (len(schedulers) - 1) / 2) * width
            values = []
            for kernel in kernels:
                match = next(
                    (row for row in rows if row["scheduler"] == scheduler_name and row["kernel"] == kernel),
                    None,
                )
                values.append(match["bandwidth"] if match else 0.0)
            ax.bar(offsets, values, width, label=scheduler_name)

        ax.set_title("HPC Memory-Bandwidth Proxy Scheduler Comparison")
        ax.set_ylabel("effective GB/s")
        ax.set_xticks(x)
        ax.set_xticklabels(kernels, rotation=25, ha="right")
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend()
        fig.tight_layout()

        png_path = self.results_dir / "hpc_memory_scheduler_comparison.png"
        pdf_path = self.results_dir / "hpc_memory_scheduler_comparison.pdf"
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
        plt.close(fig)
        print(f"Figure saved to {png_path}")
        print(f"PDF saved to {pdf_path}")
        return pdf_path

    def print_summary(self, results_by_scheduler):
        print("\n" + "=" * 50)
        print("HPC MEMORY BANDWIDTH SUMMARY")
        print("=" * 50)
        for scheduler_name, result in results_by_scheduler.items():
            if result.get("return_code") != 0:
                print(f"\n{scheduler_name}: failed")
                err = result.get("stderr", "").strip()
                if err:
                    print(f"  {err.splitlines()[-1]}")
                continue

            print(f"\n{scheduler_name}")
            for entry in result.get("hpc_memory", {}).get("results", []):
                print(
                    f"  {entry.get('kernel')}: "
                    f"{float(entry.get('bandwidth_gb_s', 0.0)):.2f} GB/s, "
                    f"{float(entry.get('seconds', 0.0)):.3f}s"
                )


def main():
    parser = argparse.ArgumentParser(description="Run HPC memory proxy scheduler benchmarks")
    parser.add_argument("--binary", default=None, help="Path to hpc_memory_kernels")
    parser.add_argument("--results-dir", default="results", help="Directory for output")
    parser.add_argument("--scheduler", default=None, help="Run one scheduler after default")
    parser.add_argument("--schedulers", nargs="+", default=None, help="Run explicit scheduler list after default")
    parser.add_argument("--skip-default", action="store_true", help="Do not run default Linux scheduler")
    parser.add_argument("--kernel", default="all",
                        help="Kernel name, 'all', or a comma-separated list such as wrf_stencil,gromacs_pairlist,sst_sparse,quantum_state")
    parser.add_argument("--threads", type=int, default=0, help="Worker threads, 0 uses OpenMP default")
    parser.add_argument("--size-mib", type=int, default=256, help="Data size per main array")
    parser.add_argument("--iterations", type=int, default=3, help="Kernel iterations")
    parser.add_argument("--slug-hint", default="pipeline",
                        choices=["read", "write", "balanced", "pipeline"],
                        help="SLUG marker hint used by worker threads")
    parser.add_argument("--quick", action="store_true", help="Use a smaller smoke-test configuration")
    args = parser.parse_args()

    if args.quick:
        args.size_mib = min(args.size_mib, 128)
        args.iterations = min(args.iterations, 2)
        if args.threads == 0:
            args.threads = min(os.cpu_count() or 4, 64)

    schedulers = args.schedulers or ([] if args.scheduler is None else [args.scheduler])
    bench = HpcMemoryBenchmark(binary=args.binary, results_dir=args.results_dir)
    results = bench.run_suite(
        schedulers=schedulers,
        include_default=not args.skip_default,
        kernel=args.kernel,
        threads=args.threads,
        size_mib=args.size_mib,
        iterations=args.iterations,
        slug_hint=args.slug_hint,
    )
    bench.print_summary(results)
    bench.generate_figure(results)


if __name__ == "__main__":
    main()
