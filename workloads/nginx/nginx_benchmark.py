#!/usr/bin/env python3
"""
Nginx Benchmark Suite using wrk2 for Scheduler Performance Evaluation
"""
import subprocess
import time
import json
import psutil
import os
import sys
import re
import argparse
import shutil
from datetime import datetime
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(repo_root / "scheduler"))

from scheduler_runner import SchedulerRunner

class NginxBenchmark:
    def __init__(self, nginx_dir="nginx", wrk2_dir="wrk2", results_dir="results",
                 scheduler_runner=None):
        self.nginx_dir = nginx_dir
        self.wrk2_dir = wrk2_dir
        self.results_dir = results_dir
        self.nginx_binary = os.path.join(nginx_dir, "objs", "nginx")
        self.nginx_config = os.path.abspath("nginx-local.conf")
        self.wrk_binary = shutil.which("wrk")
        self.load_tool = "wrk"
        if not self.wrk_binary:
            self.wrk_binary = os.path.join(wrk2_dir, "wrk")
            self.load_tool = "wrk2"
        self.runner = scheduler_runner or SchedulerRunner()
        self.nginx_process = None
        
        os.makedirs(results_dir, exist_ok=True)
        
        # Validate dependencies
        self.validate_dependencies()
    
    def validate_dependencies(self):
        """Validate that all required dependencies are available"""
        print("Validating dependencies...")
        
        # Check Nginx binary
        if not os.path.exists(self.nginx_binary):
            print(f"ERROR: Nginx binary not found at {self.nginx_binary}")
            print(f"Current working directory: {os.getcwd()}")
            print(f"Files in current directory: {os.listdir('.')}")
            sys.exit(1)
        else:
            print(f"✓ Nginx binary found at {self.nginx_binary}")
        
        # Check Nginx config
        if not os.path.exists(self.nginx_config):
            print(f"ERROR: Nginx config not found at {self.nginx_config}")
            sys.exit(1)
        else:
            print(f"✓ Nginx config found at {self.nginx_config}")
        
        # Check HTTP load generator binary
        if not os.path.exists(self.wrk_binary):
            print(f"ERROR: load generator not found at {self.wrk_binary}")
            print(f"Expected path: {os.path.abspath(self.wrk_binary)}")
            if os.path.exists(self.wrk2_dir):
                print(f"Files in {self.wrk2_dir}: {os.listdir(self.wrk2_dir)}")
            else:
                print(f"Directory {self.wrk2_dir} does not exist")
            sys.exit(1)
        else:
            print(f"✓ {self.load_tool} binary found at {self.wrk_binary}")
        
        # Check HTML directory
        html_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html")
        if not os.path.exists(html_dir):
            print(f"WARNING: HTML directory not found at {html_dir}")
            print("Creating HTML directory...")
            os.makedirs(html_dir, exist_ok=True)
            with open(os.path.join(html_dir, "index.html"), "w") as f:
                f.write("<html><body><h1>Nginx Benchmark Test Page</h1></body></html>")
        else:
            print(f"✓ HTML directory found at {html_dir}")
        
        print("All dependencies validated successfully!")
        
    def start_nginx(self):
        """Start Nginx server"""
        print("Starting Nginx server...")
        
        # First, ensure nginx is not already running
        self.stop_nginx()
        
        cmd = [self.nginx_binary, "-c", self.nginx_config]
        print(f"Running command: {' '.join(cmd)}")
        
        try:
            self.nginx_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            time.sleep(2)

            if self.nginx_process.poll() is not None:
                print(f"ERROR: Nginx exited early. Return code: {self.nginx_process.returncode}")
                self.nginx_process = None
                return False
            
            # Check if Nginx process is running
            try:
                ps_result = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, text=True)
                if ps_result.returncode == 0:
                    print(f"✓ Nginx processes found: {ps_result.stdout.strip()}")
                else:
                    print("⚠ No nginx processes found")
            except:
                print("Could not check for nginx processes")
            
            # Check if Nginx is responding
            print("Checking Nginx health...")
            try:
                health_result = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", 
                                       "http://127.0.0.1:8080/"], 
                                      capture_output=True, text=True, timeout=5)
                
                print(f"Health check HTTP status: {health_result.stdout}")
                if health_result.returncode != 0:
                    print(f"Curl failed with return code: {health_result.returncode}")
                    if health_result.stderr:
                        print(f"Curl stderr: {health_result.stderr}")
                
                if health_result.stdout == "200":
                    print("✓ Nginx server started successfully and responding")
                    return True
                else:
                    print(f"✗ Nginx health check failed: HTTP {health_result.stdout}")
                    return False
            except Exception as e:
                print(f"Health check failed with exception: {e}")
                return False
                
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"ERROR: Failed to start Nginx server: {e}")
            return False
    
    def stop_nginx(self):
        """Stop Nginx server"""
        print("Stopping any existing Nginx processes...")
        
        # Try graceful shutdown first
        if self.nginx_process and self.nginx_process.poll() is None:
            try:
                self.nginx_process.terminate()
                self.nginx_process.wait(timeout=5)
                print("✓ Terminated managed Nginx process")
            except subprocess.TimeoutExpired:
                self.nginx_process.kill()
                self.nginx_process.wait(timeout=5)
                print("✓ Killed managed Nginx process")
            finally:
                self.nginx_process = None

        try:
            result = subprocess.run([self.nginx_binary, "-s", "quit", "-c", self.nginx_config], 
                          capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print("✓ Sent graceful shutdown signal to Nginx")
            elif result.stderr:
                print(f"Graceful shutdown attempt stderr: {result.stderr}")
        except Exception as e:
            print(f"Graceful shutdown attempt failed: {e}")
        
        # Force kill if still running
        try:
            result = subprocess.run(["pkill", "-x", "nginx"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print("✓ Force killed any remaining nginx processes")
        except Exception as e:
            print(f"Force kill attempt failed: {e}")
        
        time.sleep(1)
    
    def run_wrk2_benchmark(self, test_name, threads, connections, duration, rate, url="http://127.0.0.1:8080/"):
        """Run a wrk2 benchmark test"""
        print(f"\n--- Running {test_name} benchmark ---")
        print(f"Threads: {threads}, Connections: {connections}, Duration: {duration}s, Rate: {rate} req/s")
        
        # Start monitoring
        start_time = time.time()
        start_cpu = psutil.cpu_percent(interval=1)
        start_mem = psutil.virtual_memory().percent
        
        if self.load_tool == "wrk2":
            cmd = [
                self.wrk_binary,
                f"-t{threads}",
                f"-c{connections}",
                f"-d{duration}s",
                f"-R{rate}",
                "--latency",
                url
            ]
        else:
            cmd = [
                self.wrk_binary,
                f"-t{threads}",
                f"-c{connections}",
                f"-d{duration}s",
                "--latency",
                url
            ]
        
        print(f"Running command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 30)
            print(f"wrk2 exit code: {result.returncode}")
            
            if result.returncode != 0:
                print(f"ERROR: {self.load_tool} failed with return code {result.returncode}")
                if result.stderr:
                    print(f"{self.load_tool} stderr: {result.stderr}")
            else:
                print(f"✓ {self.load_tool} completed successfully")
                
        except subprocess.TimeoutExpired:
            print(f"ERROR: {self.load_tool} benchmark timed out after {duration + 30} seconds")
            result = subprocess.CompletedProcess(cmd, -1, "", "Timeout")
        except Exception as e:
            print(f"ERROR: {self.load_tool} benchmark failed with exception: {e}")
            result = subprocess.CompletedProcess(cmd, -1, "", str(e))
        
        # End monitoring
        end_time = time.time()
        end_cpu = psutil.cpu_percent(interval=1)
        end_mem = psutil.virtual_memory().percent
        
        return {
            "test_name": test_name,
            "command": " ".join(cmd),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
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
    
    def parse_wrk2_output(self, output):
        """Parse wrk2 output for metrics"""
        metrics = {}
        
        # Parse requests per second
        rps_match = re.search(r'Requests/sec:\s+(\d+\.?\d*)', output)
        if rps_match:
            metrics['requests_per_second'] = float(rps_match.group(1))
        
        # Parse transfer rate
        transfer_match = re.search(r'Transfer/sec:\s+(\d+\.?\d*)', output)
        if transfer_match:
            metrics['transfer_per_second'] = float(transfer_match.group(1))
        
        # Parse latency statistics
        latency_patterns = [
            (r'(?:50\.000%|50%)\s+(\d+\.?\d*)(us|ms|s)', 'latency_p50'),
            (r'(?:75\.000%|75%)\s+(\d+\.?\d*)(us|ms|s)', 'latency_p75'),
            (r'(?:90\.000%|90%)\s+(\d+\.?\d*)(us|ms|s)', 'latency_p90'),
            (r'(?:99\.000%|99%)\s+(\d+\.?\d*)(us|ms|s)', 'latency_p99'),
            (r'(?:99\.900%|99\.9%)\s+(\d+\.?\d*)(us|ms|s)', 'latency_p999'),
        ]
        
        for pattern, key in latency_patterns:
            match = re.search(pattern, output)
            if match:
                value = float(match.group(1))
                unit = match.group(2)
                # Convert to milliseconds
                if unit == 'us':
                    value = value / 1000
                elif unit == 's':
                    value = value * 1000
                metrics[key] = value
        
        # Parse total requests
        total_requests_match = re.search(r'(\d+) requests in', output)
        if total_requests_match:
            metrics['total_requests'] = int(total_requests_match.group(1))
        
        # Parse errors
        error_patterns = [
            (r'Socket errors: connect (\d+)', 'connect_errors'),
            (r'read (\d+)', 'read_errors'),
            (r'write (\d+)', 'write_errors'),
            (r'timeout (\d+)', 'timeout_errors'),
        ]
        
        for pattern, key in error_patterns:
            match = re.search(pattern, output)
            if match:
                metrics[key] = int(match.group(1))
        
        return metrics
    
    def _benchmark_matrix(self, quick=False):
        """Return the wrk2 load matrix."""
        if quick:
            return [
                {
                    "name": "low_load_test",
                    "threads": 2,
                    "connections": 10,
                    "duration": 10,
                    "rate": 100
                },
                {
                    "name": "medium_load_test",
                    "threads": 4,
                    "connections": 50,
                    "duration": 10,
                    "rate": 1000
                },
                {
                    "name": "high_load_test",
                    "threads": 8,
                    "connections": 100,
                    "duration": 10,
                    "rate": 5000
                }
            ]

        return [
            {
                "name": "low_load_test",
                "threads": 2,
                "connections": 10,
                "duration": 30,
                "rate": 100
            },
            {
                "name": "medium_load_test",
                "threads": 4,
                "connections": 50,
                "duration": 30,
                "rate": 1000
            },
            {
                "name": "high_load_test",
                "threads": 8,
                "connections": 100,
                "duration": 30,
                "rate": 5000
            },
            {
                "name": "stress_test",
                "threads": 12,
                "connections": 200,
                "duration": 30,
                "rate": 10000
            },
            {
                "name": "burst_test",
                "threads": 16,
                "connections": 400,
                "duration": 60,
                "rate": 20000
            }
        ]

    def run_comprehensive_benchmark(self, scheduler_name=None, quick=False):
        """Run comprehensive Nginx benchmarks"""
        scheduler_proc = None
        scheduler_label = scheduler_name or "default"

        if scheduler_name:
            print(f"Starting scheduler: {scheduler_name}")
            scheduler_proc = self.runner.start_scheduler(scheduler_name)

        if not self.start_nginx():
            if scheduler_proc:
                self.runner.stop_scheduler(proc=scheduler_proc)
            return None

        benchmarks = self._benchmark_matrix(quick)
        
        results = []
        
        try:
            for i, benchmark in enumerate(benchmarks):
                print(f"\n=== Test {i+1}/{len(benchmarks)}: {benchmark['name']} ===")
                
                result = self.run_wrk2_benchmark(
                    benchmark["name"],
                    benchmark["threads"],
                    benchmark["connections"],
                    benchmark["duration"],
                    benchmark["rate"]
                )
                
                # Parse metrics from output
                if result["return_code"] == 0:
                    metrics = self.parse_wrk2_output(result["stdout"])
                    result["metrics"] = metrics
                    result["config"] = {
                        "threads": benchmark["threads"],
                        "connections": benchmark["connections"],
                        "duration": benchmark["duration"],
                        "target_rate": benchmark["rate"]
                    }
                    result["scheduler"] = scheduler_label
                else:
                    print(f"⚠ Benchmark {benchmark['name']} failed, but continuing...")
                    result["scheduler"] = scheduler_label
                
                results.append(result)
                time.sleep(2)  # Brief pause between tests
                
        except KeyboardInterrupt:
            print("\n\n⚠ Benchmark interrupted by user (Ctrl+C)")
            return results
        finally:
            print("\nCleaning up...")
            self.stop_nginx()
            if scheduler_proc:
                self.runner.stop_scheduler(proc=scheduler_proc)
        
        return results
    
    def save_results(self, results):
        """Save benchmark results to JSON file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"nginx_benchmark_{timestamp}.json"
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
        """Run the benchmark for default and an explicit scheduler list."""
        schedulers = schedulers or []
        results = {}

        if include_default:
            print("\n=== Scheduler: default ===")
            results["default"] = self.run_comprehensive_benchmark(
                scheduler_name=None,
                quick=quick,
            ) or []
            self.save_results(results)

        for scheduler_name in schedulers:
            print(f"\n=== Scheduler: {scheduler_name} ===")
            results[scheduler_name] = self.run_comprehensive_benchmark(
                scheduler_name=scheduler_name,
                quick=quick,
            ) or []
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
        avg_rps = []
        avg_p99 = []

        for scheduler_name, results in results_by_scheduler.items():
            successful = [
                r for r in results
                if r.get("return_code") == 0 and r.get("metrics")
            ]
            rps_values = [
                r["metrics"]["requests_per_second"] for r in successful
                if "requests_per_second" in r["metrics"]
            ]
            p99_values = [
                r["metrics"]["latency_p99"] for r in successful
                if "latency_p99" in r["metrics"]
            ]
            if not rps_values and not p99_values:
                continue

            schedulers.append(scheduler_name)
            avg_rps.append(float(np.mean(rps_values)) if rps_values else 0.0)
            avg_p99.append(float(np.mean(p99_values)) if p99_values else 0.0)

        if not schedulers:
            print("No successful Nginx results to plot")
            return None

        x = np.arange(len(schedulers))
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

        ax1.bar(x, avg_rps, color="#4C78A8")
        ax1.set_title("Average Throughput")
        ax1.set_ylabel("Requests/sec")
        ax1.set_xticks(x)
        ax1.set_xticklabels(schedulers, rotation=30, ha="right")
        ax1.grid(True, axis="y", alpha=0.3)

        ax2.bar(x, avg_p99, color="#F58518")
        ax2.set_title("Average P99 Latency")
        ax2.set_ylabel("ms")
        ax2.set_xticks(x)
        ax2.set_xticklabels(schedulers, rotation=30, ha="right")
        ax2.grid(True, axis="y", alpha=0.3)

        fig.suptitle("Nginx Scheduler Comparison")
        fig.tight_layout()

        png_path = os.path.join(self.results_dir, "nginx_scheduler_comparison.png")
        pdf_path = os.path.join(self.results_dir, "nginx_scheduler_comparison.pdf")
        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
        plt.close(fig)

        print(f"Comparison figure saved to {png_path}")
        print(f"Comparison PDF saved to {pdf_path}")
        return pdf_path

    def print_suite_summary(self, results_by_scheduler):
        """Print compact benchmark summaries for all schedulers."""
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
                if "requests_per_second" in metrics:
                    line += f", RPS={metrics['requests_per_second']:,.0f}"
                if "latency_p99" in metrics:
                    line += f", P99={metrics['latency_p99']:.2f}ms"
                print(line)

def main():
    try:
        parser = argparse.ArgumentParser(description="Run Nginx wrk2 scheduler benchmarks")
        parser.add_argument("--scheduler", default=None,
                            help="Run one scheduler after default")
        parser.add_argument("--schedulers", nargs="+", default=None,
                            help="Run explicit scheduler list after default")
        parser.add_argument("--skip-default", action="store_true",
                            help="Do not run the default Linux scheduler baseline")
        parser.add_argument("--quick", action="store_true",
                            help="Use shorter 10 second wrk2 tests")
        parser.add_argument("--results-dir", default="results",
                            help="Directory to store benchmark results")
        parser.add_argument("--test", action="store_true",
                            help="Only test Nginx startup and shutdown")
        args = parser.parse_args()

        print("Initializing NginxBenchmark...")
        benchmark = NginxBenchmark(results_dir=args.results_dir)

        if args.test:
            print("Testing Nginx startup...")
            if benchmark.start_nginx():
                print("Nginx started successfully")
                benchmark.stop_nginx()
                print("Nginx stopped successfully")
                return
            print("Failed to start Nginx")
            sys.exit(1)

        print("Starting Nginx benchmark suite with wrk2...")
        print("=" * 50)

        schedulers = args.schedulers or ([] if args.scheduler is None else [args.scheduler])
        results = benchmark.run_scheduler_suite(
            schedulers=schedulers,
            include_default=not args.skip_default,
            quick=args.quick,
        )

        if any(results.values()):
            benchmark.print_suite_summary(results)
            benchmark.generate_comparison_figure(results)
        else:
            print("No benchmark results to show")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\nBenchmark interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
