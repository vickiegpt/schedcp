#!/usr/bin/env python3
"""
RocksDB Benchmark Suite for Scheduler Performance Evaluation
"""
import subprocess
import time
import json
import psutil
import os
import sys
import re
from datetime import datetime
import shutil
import argparse
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(repo_root / "scheduler"))

from scheduler_runner import SchedulerRunner

class RocksDBBenchmark:
    def __init__(self, rocksdb_dir="rocksdb", results_dir="results",
                 scheduler_runner=None, numactl_membind=None,
                 tpcc_bench_path=None, tpcc_db_path="/tmp/rocksdb_tpcc"):
        self.rocksdb_dir = rocksdb_dir
        self.results_dir = results_dir
        self.db_bench = os.path.join(rocksdb_dir, "db_bench")
        self.tpcc_bench = tpcc_bench_path or str(Path(__file__).with_name("tpcc_query_bench"))
        self.db_path = "/tmp/rocksdb_data"
        self.tpcc_db_path = tpcc_db_path
        self.runner = scheduler_runner or SchedulerRunner()
        self.numactl_membind = numactl_membind
        
        os.makedirs(results_dir, exist_ok=True)

        if not os.path.exists(self.db_bench):
            raise FileNotFoundError(
                f"db_bench not found at {self.db_bench}; run "
                "'make -C workloads/rocksdb build' first"
            )
        
    def cleanup_db(self):
        """Clean up database directory"""
        if os.path.exists(self.db_path):
            shutil.rmtree(self.db_path)
        os.makedirs(self.db_path, exist_ok=True)

    def cleanup_tpcc_db(self):
        """Clean up TPCC-like benchmark database directory."""
        if os.path.exists(self.tpcc_db_path):
            shutil.rmtree(self.tpcc_db_path)
        os.makedirs(self.tpcc_db_path, exist_ok=True)
    
    def run_db_bench(self, test_name, benchmark_args, scheduler_name=None,
                     timeout=600):
        """Run a specific db_bench test"""
        print(f"Running {test_name} benchmark...")
        
        # Clean database before each test
        self.cleanup_db()
        
        # Start monitoring
        start_time = time.time()
        start_cpu = psutil.cpu_percent(interval=1)
        start_mem = psutil.virtual_memory().percent
        
        # Base arguments for minimal logging
        base_args = [
            self.db_bench,
            f"--db={self.db_path}",
            "--disable_wal=true",
            "--statistics=false",
            "--histogram=false",
            "--compression_type=none",
        ]
        
        # Run benchmark
        cmd = base_args + benchmark_args
        if self.numactl_membind is not None:
            cmd = ["numactl", "-m", str(self.numactl_membind)] + cmd
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
                exit_code, stdout, stderr = (
                    result.returncode,
                    result.stdout,
                    result.stderr,
                )
            except subprocess.TimeoutExpired:
                exit_code, stdout, stderr = -1, "", "Command timed out"
        
        # End monitoring
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=1)
        end_mem = psutil.virtual_memory().percent
        
        return {
            "test_name": test_name,
            "command": " ".join(cmd),
            "stdout": stdout,
            "stderr": stderr,
            "return_code": exit_code,
            "scheduler": scheduler_name or "default",
            "duration": end_time - start_time,
            "cpu_usage": {
                "start": start_cpu,
                "end": end_cpu,
                "average": (start_cpu + end_cpu) / 2
            },
            "memory_usage": {
                "start": start_mem,
                "end": end_mem,
                "average": (start_mem + end_mem) / 2
            },
            "timestamp": datetime.now().isoformat()
        }
    
    def parse_db_bench_output(self, output):
        """Parse db_bench output for performance metrics"""
        metrics = {}
        
        # Parse micros/op
        micros_pattern = r'(\w+)\s*:\s*([\d.]+)\s*micros/op\s*(\d+)\s*ops/sec'
        for match in re.finditer(micros_pattern, output):
            operation = match.group(1).lower()
            micros_per_op = float(match.group(2))
            ops_per_sec = int(match.group(3))
            
            metrics[f"{operation}_micros_per_op"] = micros_per_op
            metrics[f"{operation}_ops_per_sec"] = ops_per_sec
        
        # Parse overall throughput
        throughput_pattern = r'(\d+)\s+ops/sec'
        throughput_matches = re.findall(throughput_pattern, output)
        if throughput_matches:
            metrics['overall_ops_per_sec'] = int(throughput_matches[-1])
        
        # Parse database size
        size_pattern = r'DB size:\s*([\d.]+)\s*([KMGT]?B)'
        size_match = re.search(size_pattern, output)
        if size_match:
            size_value = float(size_match.group(1))
            size_unit = size_match.group(2)
            
            # Convert to bytes
            multipliers = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
            if size_unit in multipliers:
                metrics['db_size_bytes'] = int(size_value * multipliers[size_unit])
        
        # Parse write amplification
        wa_pattern = r'Write amplification:\s*([\d.]+)'
        wa_match = re.search(wa_pattern, output)
        if wa_match:
            metrics['write_amplification'] = float(wa_match.group(1))
        
        # Parse read amplification  
        ra_pattern = r'Read amplification:\s*([\d.]+)'
        ra_match = re.search(ra_pattern, output)
        if ra_match:
            metrics['read_amplification'] = float(ra_match.group(1))
        
        return metrics
    
    def _benchmark_matrix(self, quick=False):
        if quick:
            return [
                {
                    "name": "sequential_write",
                    "args": [
                        "--benchmarks=fillseq",
                        "--num=100000",
                        "--value_size=100"
                    ]
                },
                {
                    "name": "random_read",
                    "args": [
                        "--benchmarks=fillrandom,readrandom",
                        "--num=100000",
                        "--reads=200000",
                        "--value_size=100"
                    ]
                },
                {
                    "name": "mixed_workload",
                    "args": [
                        "--benchmarks=fillrandom,readwhilewriting",
                        "--num=100000",
                        "--reads=200000",
                        "--value_size=100",
                        "--threads=4"
                    ]
                },
            ]

        return [
            {
                "name": "sequential_write",
                "args": [
                    "--benchmarks=fillseq",
                    "--num=1000000",
                    "--value_size=100"
                ]
            },
            {
                "name": "random_write",
                "args": [
                    "--benchmarks=fillrandom",
                    "--num=500000",
                    "--value_size=100"
                ]
            },
            {
                "name": "sequential_read",
                "args": [
                    "--benchmarks=fillseq,readseq",
                    "--num=500000",
                    "--value_size=100",
                    "--use_existing_db=true"
                ]
            },
            {
                "name": "random_read",
                "args": [
                    "--benchmarks=fillrandom,readrandom",
                    "--num=500000",
                    "--reads=1000000",
                    "--value_size=100"
                ]
            },
            {
                "name": "mixed_workload",
                "args": [
                    "--benchmarks=fillrandom,readwhilewriting",
                    "--num=300000",
                    "--reads=500000",
                    "--value_size=100",
                    "--threads=4"
                ]
            },
            {
                "name": "compression_test",
                "args": [
                    "--benchmarks=fillrandom,stats",
                    "--num=200000",
                    "--value_size=1000",
                    "--compression_type=lz4"
                ]
            },
            {
                "name": "bulk_load",
                "args": [
                    "--benchmarks=bulkload",
                    "--num=1000000",
                    "--value_size=100",
                    "--batch_size=1000"
                ]
            },
            {
                "name": "range_scan",
                "args": [
                    "--benchmarks=fillrandom,seekrandom",
                    "--num=200000",
                    "--seeks=100000",
                    "--value_size=100"
                ]
            }
        ]

    def run_comprehensive_benchmark(self, scheduler_name=None, quick=False):
        """Run comprehensive RocksDB benchmarks"""
        benchmarks = self._benchmark_matrix(quick)
        
        results = []
        
        for benchmark in benchmarks:
            result = self.run_db_bench(
                benchmark["name"],
                benchmark["args"],
                scheduler_name=scheduler_name,
            )
            
            # Parse metrics from output
            if result["return_code"] == 0:
                metrics = self.parse_db_bench_output(result["stdout"])
                result["metrics"] = metrics
                result["config"] = {
                    "args": benchmark["args"]
                }
            
            results.append(result)
            time.sleep(1)  # Brief pause between tests
        
        return results

    def run_tpcc_query_bench(self, scheduler_name=None, query="all",
                             transactions=20000, threads=4, warehouses=4,
                             districts=8, customers=1000, items=10000,
                             timeout=900):
        """Run the synthetic TPC-C-like per-query RocksDB benchmark."""
        if not os.path.exists(self.tpcc_bench):
            raise FileNotFoundError(
                f"TPCC-like benchmark not found at {self.tpcc_bench}; run "
                "'make -C workloads/rocksdb tpcc-query-bench' first"
            )

        self.cleanup_tpcc_db()
        cmd = [
            self.tpcc_bench,
            f"--db={self.tpcc_db_path}",
            f"--query={query}",
            f"--transactions={transactions}",
            f"--threads={threads}",
            f"--warehouses={warehouses}",
            f"--districts={districts}",
            f"--customers={customers}",
            f"--items={items}",
        ]
        if self.numactl_membind is not None:
            cmd = ["numactl", "-m", str(self.numactl_membind)] + cmd

        start_time = time.time()
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
                json_start = stdout.find("{")
                parsed = json.loads(stdout[json_start:] if json_start >= 0 else stdout)
            except json.JSONDecodeError as e:
                exit_code = -1
                stderr = f"Failed to parse TPCC JSON: {e}\n{stderr}"

        return {
            "test_name": "tpcc_query",
            "command": " ".join(cmd),
            "stdout": stdout,
            "stderr": stderr,
            "return_code": exit_code,
            "scheduler": scheduler_name or "default",
            "duration": time.time() - start_time,
            "timestamp": datetime.now().isoformat(),
            "tpcc": parsed,
        }

    def run_tpcc_scheduler_suite(self, schedulers=None, include_default=True,
                                 query="all", transactions=20000, threads=4,
                                 warehouses=4, districts=8, customers=1000,
                                 items=10000):
        """Run TPCC-like per-query benchmarks for default and scheduler list."""
        schedulers = schedulers or []
        results = {}

        if include_default:
            print("\n=== TPCC Scheduler: default ===")
            results["default"] = self.run_tpcc_query_bench(
                scheduler_name=None,
                query=query,
                transactions=transactions,
                threads=threads,
                warehouses=warehouses,
                districts=districts,
                customers=customers,
                items=items,
            )
            self.save_results(results)

        for scheduler_name in schedulers:
            print(f"\n=== TPCC Scheduler: {scheduler_name} ===")
            results[scheduler_name] = self.run_tpcc_query_bench(
                scheduler_name=scheduler_name,
                query=query,
                transactions=transactions,
                threads=threads,
                warehouses=warehouses,
                districts=districts,
                customers=customers,
                items=items,
            )
            self.save_results(results)
            time.sleep(1)

        return results
    
    def save_results(self, results):
        """Save benchmark results to JSON file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"rocksdb_benchmark_{timestamp}.json"
        filepath = os.path.join(self.results_dir, filename)
        
        with open(filepath, 'w') as f:
            json.dump(results, f, indent=2)
        
        print(f"Results saved to {filepath}")
        return filepath
    
    def generate_summary(self, results):
        """Generate benchmark summary"""
        summary = {
            "total_tests": len(results),
            "successful_tests": sum(1 for r in results if r["return_code"] == 0),
            "failed_tests": sum(1 for r in results if r["return_code"] != 0),
            "total_duration": sum(r["duration"] for r in results),
            "average_cpu_usage": sum(r["cpu_usage"]["average"] for r in results) / len(results),
            "average_memory_usage": sum(r["memory_usage"]["average"] for r in results) / len(results),
            "test_summary": []
        }
        
        for result in results:
            test_summary = {
                "test_name": result["test_name"],
                "status": "success" if result["return_code"] == 0 else "failed",
                "duration": result["duration"],
                "metrics": result.get("metrics", {}),
                "config": result.get("config", {})
            }
            summary["test_summary"].append(test_summary)
        
        return summary

    def run_scheduler_suite(self, schedulers=None, include_default=True, quick=False):
        """Run RocksDB benchmarks for default and an explicit scheduler list."""
        schedulers = schedulers or []
        results = {}

        if include_default:
            print("\n=== Scheduler: default ===")
            results["default"] = self.run_comprehensive_benchmark(
                scheduler_name=None,
                quick=quick,
            )
            self.save_results(results)

        for scheduler_name in schedulers:
            print(f"\n=== Scheduler: {scheduler_name} ===")
            results[scheduler_name] = self.run_comprehensive_benchmark(
                scheduler_name=scheduler_name,
                quick=quick,
            )
            self.save_results(results)

        return results

    def generate_comparison_figure(self, results_by_scheduler):
        """Generate PNG and PDF summaries for scheduler comparison results."""
        try:
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as e:
            print(f"Could not generate figures: {e}")
            return None

        schedulers = []
        avg_ops = []
        avg_micros = []

        for scheduler_name, results in results_by_scheduler.items():
            successful = [
                r for r in results
                if r.get("return_code") == 0 and r.get("metrics")
            ]
            ops_values = []
            micros_values = []

            for result in successful:
                metrics = result["metrics"]
                if "overall_ops_per_sec" in metrics:
                    ops_values.append(metrics["overall_ops_per_sec"])
                for key, value in metrics.items():
                    if key.endswith("_micros_per_op"):
                        micros_values.append(value)

            if not ops_values and not micros_values:
                continue

            schedulers.append(scheduler_name)
            avg_ops.append(float(np.mean(ops_values)) if ops_values else 0.0)
            avg_micros.append(float(np.mean(micros_values)) if micros_values else 0.0)

        if not schedulers:
            print("No successful RocksDB results to plot")
            return None

        x = np.arange(len(schedulers))
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        ax1.bar(x, avg_ops, color="#4C78A8")
        ax1.set_title("Average Throughput")
        ax1.set_ylabel("ops/sec")
        ax1.set_xticks(x)
        ax1.set_xticklabels(schedulers, rotation=30, ha="right")
        ax1.grid(True, axis="y", alpha=0.3)

        ax2.bar(x, avg_micros, color="#F58518")
        ax2.set_title("Average Operation Latency")
        ax2.set_ylabel("micros/op")
        ax2.set_xticks(x)
        ax2.set_xticklabels(schedulers, rotation=30, ha="right")
        ax2.grid(True, axis="y", alpha=0.3)

        fig.suptitle("RocksDB Scheduler Comparison")
        fig.tight_layout()

        png_path = os.path.join(self.results_dir, "rocksdb_scheduler_comparison.png")
        pdf_path = os.path.join(self.results_dir, "rocksdb_scheduler_comparison.pdf")
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
        plt.close(fig)

        print(f"Comparison figure saved to {png_path}")
        print(f"Comparison PDF saved to {pdf_path}")
        return pdf_path

    def generate_tpcc_comparison_figure(self, results_by_scheduler):
        """Generate grouped per-query TPCC-like scheduler comparison figures."""
        try:
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError as e:
            print(f"Could not generate TPCC figures: {e}")
            return None

        query_order = ["new_order", "payment", "order_status", "delivery", "stock_level"]
        rows = []
        for scheduler_name, result in results_by_scheduler.items():
            if result.get("return_code") != 0:
                continue
            for entry in result.get("tpcc", {}).get("results", []):
                rows.append({
                    "scheduler": scheduler_name,
                    "query": entry.get("query"),
                    "tps": float(entry.get("tps", 0.0)),
                    "avg_micros": float(entry.get("avg_micros", 0.0)),
                    "read_ops_per_tx": float(entry.get("read_ops_per_tx", 0.0)),
                    "write_ops_per_tx": float(entry.get("write_ops_per_tx", 0.0)),
                })

        if not rows:
            print("No successful TPCC-like RocksDB results to plot")
            return None

        schedulers = list(dict.fromkeys(row["scheduler"] for row in rows))
        present_queries = [q for q in query_order if any(row["query"] == q for row in rows)]
        x = np.arange(len(present_queries))
        width = min(0.8 / max(1, len(schedulers)), 0.22)

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5))
        for idx, scheduler_name in enumerate(schedulers):
            offsets = x + (idx - (len(schedulers) - 1) / 2) * width
            tps_values = []
            micros_values = []
            for query in present_queries:
                match = next(
                    (row for row in rows if row["scheduler"] == scheduler_name and row["query"] == query),
                    None,
                )
                tps_values.append(match["tps"] if match else 0.0)
                micros_values.append(match["avg_micros"] if match else 0.0)
            ax1.bar(offsets, tps_values, width, label=scheduler_name)
            ax2.bar(offsets, micros_values, width, label=scheduler_name)

        for ax, ylabel, title in (
            (ax1, "transactions/sec", "Per-Query Throughput"),
            (ax2, "micros/transaction", "Per-Query Latency"),
        ):
            ax.set_xticks(x)
            ax.set_xticklabels(present_queries, rotation=25, ha="right")
            ax.set_ylabel(ylabel)
            ax.set_title(title)
            ax.grid(True, axis="y", alpha=0.3)

        ax1.legend()
        fig.suptitle("RocksDB TPCC-like Per-Query Scheduler Comparison")
        fig.tight_layout()

        png_path = os.path.join(self.results_dir, "rocksdb_tpcc_query_comparison.png")
        pdf_path = os.path.join(self.results_dir, "rocksdb_tpcc_query_comparison.pdf")
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
        plt.close(fig)

        print(f"TPCC-like comparison figure saved to {png_path}")
        print(f"TPCC-like comparison PDF saved to {pdf_path}")
        return pdf_path

    def print_tpcc_summary(self, results_by_scheduler):
        print("\n" + "=" * 50)
        print("ROCKSDB TPCC-LIKE QUERY SUMMARY")
        print("=" * 50)

        for scheduler_name, result in results_by_scheduler.items():
            if result.get("return_code") != 0:
                print(f"\n{scheduler_name}: failed")
                err = result.get("stderr", "").strip()
                if err:
                    print(f"  {err.splitlines()[-1]}")
                continue

            print(f"\n{scheduler_name}")
            for entry in result.get("tpcc", {}).get("results", []):
                print(
                    f"  {entry.get('query')}: "
                    f"TPS={float(entry.get('tps', 0.0)):,.1f}, "
                    f"avg={float(entry.get('avg_micros', 0.0)):.2f} us"
                )

    def print_suite_summary(self, results_by_scheduler):
        print("\n" + "=" * 50)
        print("BENCHMARK SUMMARY")
        print("=" * 50)

        for scheduler_name, results in results_by_scheduler.items():
            if not results:
                print(f"\n{scheduler_name}: no benchmark results")
                continue

            summary = self.generate_summary(results)
            print(f"\n{scheduler_name}")
            print(f"  Total tests: {summary['total_tests']}")
            print(f"  Successful: {summary['successful_tests']}")
            print(f"  Failed: {summary['failed_tests']}")
            print(f"  Total duration: {summary['total_duration']:.2f} seconds")

            for test in summary["test_summary"]:
                metrics = test.get("metrics", {})
                line = f"  {test['test_name']}: {test['status']}"
                if "overall_ops_per_sec" in metrics:
                    line += f", OPS={metrics['overall_ops_per_sec']:,.0f}"
                print(line)

def main():
    parser = argparse.ArgumentParser(description="Run RocksDB db_bench scheduler benchmarks")
    parser.add_argument("--scheduler", default=None,
                        help="Run one scheduler after default")
    parser.add_argument("--schedulers", nargs="+", default=None,
                        help="Run explicit scheduler list after default")
    parser.add_argument("--skip-default", action="store_true",
                        help="Do not run the default Linux scheduler baseline")
    parser.add_argument("--quick", action="store_true",
                        help="Use a reduced benchmark matrix")
    parser.add_argument("--rocksdb-dir", default="rocksdb",
                        help="Directory containing db_bench")
    parser.add_argument("--results-dir", default="results",
                        help="Directory to store benchmark results")
    parser.add_argument("--numactl-membind", default=None,
                        help="Run db_bench under numactl -m NODE")
    parser.add_argument("--tpcc", action="store_true",
                        help="Run the TPCC-like per-query RocksDB benchmark")
    parser.add_argument("--tpcc-query", default="all",
                        choices=["all", "new_order", "payment", "order_status", "delivery", "stock_level"],
                        help="TPCC-like query/transaction type to run")
    parser.add_argument("--tpcc-transactions", type=int, default=20000,
                        help="Transactions per TPCC-like query type")
    parser.add_argument("--tpcc-threads", type=int, default=4,
                        help="Threads for TPCC-like query benchmark")
    parser.add_argument("--tpcc-warehouses", type=int, default=4,
                        help="Synthetic warehouse count")
    parser.add_argument("--tpcc-districts", type=int, default=8,
                        help="Synthetic district count per warehouse")
    parser.add_argument("--tpcc-customers", type=int, default=1000,
                        help="Synthetic customer count per district")
    parser.add_argument("--tpcc-items", type=int, default=10000,
                        help="Synthetic item count")
    parser.add_argument("--tpcc-bench-path", default=None,
                        help="Path to tpcc_query_bench binary")
    parser.add_argument("--tpcc-db-path", default="/tmp/rocksdb_tpcc",
                        help="Database path for TPCC-like query benchmark")
    args = parser.parse_args()

    benchmark = RocksDBBenchmark(
        rocksdb_dir=args.rocksdb_dir,
        results_dir=args.results_dir,
        numactl_membind=args.numactl_membind,
        tpcc_bench_path=args.tpcc_bench_path,
        tpcc_db_path=args.tpcc_db_path,
    )
    
    print("Starting RocksDB benchmark suite...")
    print("=" * 50)
    
    schedulers = args.schedulers or ([] if args.scheduler is None else [args.scheduler])

    if args.tpcc:
        results = benchmark.run_tpcc_scheduler_suite(
            schedulers=schedulers,
            include_default=not args.skip_default,
            query=args.tpcc_query,
            transactions=args.tpcc_transactions,
            threads=args.tpcc_threads,
            warehouses=args.tpcc_warehouses,
            districts=args.tpcc_districts,
            customers=args.tpcc_customers,
            items=args.tpcc_items,
        )
        if any(results.values()):
            benchmark.print_tpcc_summary(results)
            benchmark.generate_tpcc_comparison_figure(results)
        else:
            print("Failed to run TPCC-like benchmarks")
            sys.exit(1)
        return

    results = benchmark.run_scheduler_suite(
        schedulers=schedulers,
        include_default=not args.skip_default,
        quick=args.quick,
    )
    
    if any(results.values()):
        benchmark.print_suite_summary(results)
        benchmark.generate_comparison_figure(results)
    
    else:
        print("Failed to run benchmarks")
        sys.exit(1)

if __name__ == "__main__":
    main()
