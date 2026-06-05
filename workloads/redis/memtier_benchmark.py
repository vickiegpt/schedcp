#!/usr/bin/env python3
"""
Memtier Benchmark Suite for Redis Performance Evaluation
"""
import subprocess
import time
import json
import os
import sys
import signal
import atexit
import traceback
from datetime import datetime

# Import utility functions
from utils import (
    RedisCleanup,
    RedisConfig,
    ProcessManager
)

class MemtierBenchmark:
    def __init__(self, redis_dir="redis-src", results_dir="results", config_options=None):
        print(f"[INFO] Initializing MemtierBenchmark with redis_dir={redis_dir}, results_dir={results_dir}")
        
        self.redis_dir = redis_dir
        self.results_dir = results_dir
        self.redis_cli = os.path.join(redis_dir, "src", "redis-cli")
        self.redis_server = os.path.join(redis_dir, "src", "redis-server")
        self.memtier_benchmark = "./memtier_benchmark/memtier_benchmark"
        self.redis_process = None
        self.config_file = None
        self.config_options = config_options or {}
        
        print(f"[INFO] Redis configuration options: {self.config_options}")
        
        # Check if binaries exist
        binaries = [self.redis_server, self.redis_cli, self.memtier_benchmark]
        missing_binaries = []
        
        for binary in binaries:
            if not os.path.exists(binary):
                missing_binaries.append(binary)
                print(f"[ERROR] Missing binary: {binary}")
        
        if missing_binaries:
            error_msg = f"Required binaries not found: {missing_binaries}"
            print(f"[ERROR] {error_msg}")
            print("Please run 'make build' first to compile Redis and memtier_benchmark")
            sys.exit(1)
        
        print("[INFO] All required binaries found successfully")
        
        # Validate and create results directory
        try:
            os.makedirs(results_dir, exist_ok=True)
            print(f"[INFO] Results directory ensured: {results_dir}")
        except Exception as e:
            print(f"[ERROR] Failed to create results directory {results_dir}: {e}")
            raise
        
        # Register cleanup on exit
        atexit.register(self.cleanup)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        print("[INFO] Signal handlers and cleanup registered")
    
    def _signal_handler(self, signum, frame):
        """Handle interrupt signals gracefully"""
        print(f"\n[INFO] Signal {signum} received, initiating cleanup")
        self.cleanup()
        sys.exit(0)
    
    def cleanup(self):
        """Ensure Redis is stopped on exit and clean up all temporary files"""
        print("[INFO] Starting cleanup process")
        
        if hasattr(self, 'redis_process') and self.redis_process:
            print("[INFO] Stopping Redis process during cleanup")
            self.stop_redis()
        
        # Clean up temporary config file
        if self.config_file and os.path.exists(self.config_file):
            try:
                os.remove(self.config_file)
                print(f"[INFO] Removed temporary config file: {self.config_file}")
            except Exception as e:
                print(f"[WARNING] Failed to remove config file {self.config_file}: {e}")
        
        # Use utility function for comprehensive cleanup
        RedisCleanup.cleanup_redis_files()
        
        print("[INFO] Cleanup completed")
        
    def generate_config(self):
        """Generate Redis configuration file"""
        print("[INFO] Generating Redis configuration file")
        
        # Use utility function to generate config
        self.config_file = RedisConfig.generate_config_file(self.config_options)
        return self.config_file
    
    def start_redis(self):
        """Start Redis server"""
        print("[INFO] Starting Redis server...")
        
        # First ensure no Redis is running
        self.stop_redis()
        time.sleep(1)
        
        # Generate configuration file
        try:
            config_path = self.generate_config()
            print(f"[INFO] Generated Redis config file: {config_path}")
        except Exception as e:
            print(f"[ERROR] Failed to generate Redis configuration: {e}")
            return False
        
        # Print configuration being used
        print(f"Redis configuration:")
        if self.config_options:
            for key, value in self.config_options.items():
                print(f"  {key}: {value}")
        else:
            print("  Using default Redis configuration")
        
        cmd = [self.redis_server, config_path]
        print(f"[INFO] Starting Redis with command: {' '.join(cmd)}")
        
        try:
            self.redis_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            print(f"[INFO] Redis process started with PID: {self.redis_process.pid}")
        except Exception as e:
            print(f"[ERROR] Failed to start Redis process: {e}")
            return False
        
        # Wait for Redis to start with retries
        max_retries = 10
        for i in range(max_retries):
            time.sleep(0.5)
            if ProcessManager.check_redis_running(self.redis_cli):
                print(f"[INFO] Redis server started successfully")
                return True
            else:
                if i == max_retries - 1:
                    print("[ERROR] Failed to start Redis server after multiple attempts")
                    return False
        return False
    
    def stop_redis(self):
        """Stop Redis server"""
        print("[INFO] Stopping Redis server")
        
        # Try graceful shutdown first
        ProcessManager.stop_redis_gracefully(self.redis_cli)
        time.sleep(0.5)
        
        # Kill the process if it's still running
        if hasattr(self, 'redis_process') and self.redis_process:
            try:
                self.redis_process.terminate()
                self.redis_process.wait(timeout=2)
                print("[INFO] Redis process terminated successfully")
            except subprocess.TimeoutExpired:
                print("[WARNING] Redis process termination timed out, forcing kill")
                try:
                    self.redis_process.kill()
                    self.redis_process.wait(timeout=1)
                    print("[INFO] Redis process killed successfully")
                except Exception as e:
                    print(f"[ERROR] Failed to kill Redis process: {e}")
            except Exception as e:
                print(f"[ERROR] Error terminating Redis process: {e}")
            self.redis_process = None
        
        # Final cleanup - kill any remaining redis-server processes
        RedisCleanup.kill_redis_processes()
    
    def parse_memtier_output(self, output):
        """Parse memtier_benchmark output for metrics"""
        metrics = {}
        
        try:
            lines = output.split('\n')
            
            for line in lines:
                line = line.strip()
                
                # Parse from table format - look for "Totals" line
                if line.startswith('Totals'):
                    parts = line.split()
                    if len(parts) >= 11:
                        try:
                            metrics['ops_per_second'] = float(parts[1])
                            metrics['hits_per_second'] = float(parts[2])
                            metrics['misses_per_second'] = float(parts[3])
                            metrics['avg_latency_ms'] = float(parts[4])
                            metrics['p50_latency_ms'] = float(parts[5])
                            metrics['p99_latency_ms'] = float(parts[6])
                            metrics['p999_latency_ms'] = float(parts[7])
                            metrics['bandwidth_kb_sec'] = float(parts[8])
                        except (ValueError, IndexError) as e:
                            print(f"[DEBUG] Error parsing Totals line: {e}")
                
                # Parse from table format - look for "Gets" line for read performance
                if line.startswith('Gets'):
                    parts = line.split()
                    if len(parts) >= 8:
                        try:
                            metrics['gets_ops_per_second'] = float(parts[1])
                            metrics['gets_avg_latency_ms'] = float(parts[4])
                            metrics['gets_p50_latency_ms'] = float(parts[5])
                            metrics['gets_p99_latency_ms'] = float(parts[6])
                        except (ValueError, IndexError) as e:
                            print(f"[DEBUG] Error parsing Gets line: {e}")
                
                # Parse from table format - look for "Sets" line for write performance
                if line.startswith('Sets'):
                    parts = line.split()
                    if len(parts) >= 8:
                        try:
                            metrics['sets_ops_per_second'] = float(parts[1])
                            metrics['sets_avg_latency_ms'] = float(parts[4])
                            metrics['sets_p50_latency_ms'] = float(parts[5])
                            metrics['sets_p99_latency_ms'] = float(parts[6])
                        except (ValueError, IndexError) as e:
                            print(f"[DEBUG] Error parsing Sets line: {e}")
        
        except Exception as e:
            print(f"[WARNING] Error parsing memtier output: {e}")
        
        return metrics
    
    def run_benchmark(self, test_name, command_args):
        """Run a specific benchmark test"""
        print(f"\n[INFO] Running benchmark: {test_name}")
        
        start_time = time.time()
        
        # Run benchmark
        cmd = [self.memtier_benchmark] + command_args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            end_time = time.time()
            duration = end_time - start_time
            
            if result.returncode != 0:
                print(f"[ERROR] Benchmark {test_name} failed with return code {result.returncode}")
                print(f"[ERROR] Stderr: {result.stderr}")
            else:
                print(f"[INFO] Benchmark {test_name} completed successfully in {duration:.2f}s")
                
        except subprocess.TimeoutExpired:
            end_time = time.time()
            duration = end_time - start_time
            print(f"[ERROR] Benchmark {test_name} timed out after {duration:.2f}s")
            result = subprocess.CompletedProcess(cmd, -1, "Benchmark timed out", "Timeout")
        except Exception as e:
            end_time = time.time()
            duration = end_time - start_time
            print(f"[ERROR] Benchmark {test_name} failed with exception after {duration:.2f}s: {e}")
            result = subprocess.CompletedProcess(cmd, -1, f"Exception: {str(e)}", str(e))
        
        # Parse metrics from output
        parsed_metrics = None
        if result.stdout:
            try:
                parsed_metrics = self.parse_memtier_output(result.stdout)
                if parsed_metrics:
                    print(f"[INFO] Successfully parsed {len(parsed_metrics)} metrics")
                    # Display key metrics
                    if 'ops_per_second' in parsed_metrics:
                        print(f"  → Total: {parsed_metrics['ops_per_second']:,.0f} ops/sec")
                    if 'gets_ops_per_second' in parsed_metrics:
                        print(f"  → GET: {parsed_metrics['gets_ops_per_second']:,.0f} ops/sec, {parsed_metrics.get('gets_p99_latency_ms', 0):.2f}ms p99")
                    if 'sets_ops_per_second' in parsed_metrics:
                        print(f"  → SET: {parsed_metrics['sets_ops_per_second']:,.0f} ops/sec, {parsed_metrics.get('sets_p99_latency_ms', 0):.2f}ms p99")
            except Exception as e:
                print(f"[ERROR] Failed to parse output: {e}")
        
        # Parse results
        result_dict = {
            "test_name": test_name,
            "command": " ".join(cmd),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
            "duration": duration,
            "metrics": parsed_metrics,
            "timestamp": datetime.now().isoformat()
        }
        
        return result_dict
    
    def run_comprehensive_benchmark(self, clients=50, threads=4, requests=100000, data_size=32, 
                                   pipeline=1, ratio="1:10", key_pattern="R:R", key_maximum=10000000):
        """Run comprehensive memtier benchmarks"""
        print(f"\n{'='*60}")
        print("Starting Comprehensive Benchmark")
        print(f"{'='*60}")
        print(f"Configuration:")
        print(f"  Clients: {clients}")
        print(f"  Threads: {threads}")
        print(f"  Requests: {requests}")
        print(f"  Data size: {data_size} bytes")
        print(f"  Pipeline: {pipeline}")
        print(f"  Key pattern: {key_pattern}")
        print(f"  Redis config: {self.config_options}")
        print(f"{'='*60}")
        
        if not self.start_redis():
            print("[ERROR] Failed to start Redis for comprehensive benchmark")
            return None
        
        # Build base arguments
        base_args = [
            "-p", "6379",
            "-c", str(clients),
            "-t", str(threads),
            "-d", str(data_size),
            "--pipeline", str(pipeline),
            "--key-pattern", key_pattern,
            "--key-maximum", str(key_maximum),
            "--hide-histogram"
        ]
        
        # Define test configurations with different workload patterns
        benchmarks = [
            {
                "name": "mixed_1_10",
                "args": base_args + ["--ratio", "1:10", "-n", str(requests)]
            },
            {
                "name": "mixed_10_1",
                "args": base_args + ["--ratio", "10:1", "-n", str(requests)]
            },
            {
                "name": "pipeline_16",
                "args": ["-p", "6379", "-c", str(clients), "-t", str(threads),
                        "-d", str(data_size), "--pipeline", "16", "--key-pattern", key_pattern,
                        "--key-maximum", str(key_maximum), "--ratio", ratio,
                        "-n", str(requests), "--hide-histogram"]
            },
            {
                "name": "sequential_pattern",
                "args": ["-p", "6379", "-c", str(clients), "-t", str(threads),
                        "-d", str(data_size), "--pipeline", str(pipeline), "--key-pattern", "S:S",
                        "--key-maximum", str(key_maximum), "--ratio", ratio,
                        "-n", str(requests), "--hide-histogram"]
            },
            {
                "name": "gaussian_pattern",
                "args": ["-p", "6379", "-c", str(clients), "-t", str(threads),
                        "-d", str(data_size), "--pipeline", str(pipeline), "--key-pattern", "G:G",
                        "--key-maximum", str(key_maximum), "--ratio", ratio,
                        "-n", str(requests), "--hide-histogram"]
            },
            {
                "name": "advanced_gaussian_random",
                "args": ["-p", "6379", "-c", str(clients), "-t", str(threads),
                        "--random-data", "--data-size-range", "4-2048", "--data-size-pattern", "S",
                        "--key-minimum", "200", "--key-maximum", "400", "--key-pattern", "G:G",
                        "--key-stddev", "10", "--key-median", "300",
                        "--pipeline", str(pipeline), "--ratio", ratio,
                        "-n", str(int(requests/2)), "--hide-histogram"]
            }
        ]
        
        results = []
        print(f"\nRunning {len(benchmarks)} benchmark tests...")
        
        try:
            for i, benchmark in enumerate(benchmarks, 1):
                print(f"\n[{i}/{len(benchmarks)}] {benchmark['name']}")
                print("-" * 40)
                
                try:
                    result = self.run_benchmark(benchmark["name"], benchmark["args"])
                    results.append(result)
                    
                except Exception as e:
                    print(f"[ERROR] Exception during benchmark {benchmark['name']}: {e}")
                    error_result = {
                        "test_name": benchmark["name"],
                        "command": " ".join([self.memtier_benchmark] + benchmark["args"]),
                        "stdout": "",
                        "stderr": str(e),
                        "return_code": -1,
                        "duration": 0,
                        "metrics": None,
                        "timestamp": datetime.now().isoformat()
                    }
                    results.append(error_result)
                
                time.sleep(1)  # Brief pause between tests
                
        except Exception as e:
            print(f"[ERROR] Critical error during comprehensive benchmark: {e}")
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            return None
        finally:
            print("\n[INFO] Stopping Redis after comprehensive benchmark")
            self.stop_redis()
        
        successful_tests = sum(1 for r in results if r.get('return_code') == 0)
        print(f"\n{'='*60}")
        print(f"Benchmark completed: {successful_tests}/{len(results)} tests successful")
        print(f"{'='*60}")
        
        return results
    
    def save_results(self, results):
        """Save benchmark results to JSON file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"memtier_benchmark_{timestamp}.json"
        filepath = os.path.join(self.results_dir, filename)
        
        try:
            os.makedirs(self.results_dir, exist_ok=True)
            
            with open(filepath, 'w') as f:
                json.dump(results, f, indent=2)
            
            print(f"[INFO] Results saved to {filepath}")
            return filepath
        except Exception as e:
            print(f"[ERROR] Failed to save results: {e}")
            return None
    
    def generate_summary(self, results):
        """Generate benchmark summary"""
        if not results:
            return {"total_tests": 0, "successful_tests": 0, "failed_tests": 0, "total_duration": 0}
        
        successful_tests = sum(1 for r in results if r.get('return_code') == 0)
        failed_tests = len(results) - successful_tests
        total_duration = sum(r.get('duration', 0) for r in results)
        
        test_summary = []
        for result in results:
            test_info = {
                'test_name': result.get('test_name', 'Unknown'),
                'status': 'success' if result.get('return_code') == 0 else 'failed',
                'duration': result.get('duration', 0),
                'metrics': result.get('metrics', {})
            }
            test_summary.append(test_info)
        
        return {
            'total_tests': len(results),
            'successful_tests': successful_tests,
            'failed_tests': failed_tests,
            'total_duration': total_duration,
            'redis_config': self.config_options,
            'test_summary': test_summary
        }

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Memtier Benchmark Suite for Redis')
    parser.add_argument('--redis-dir', default='redis-src', help='Redis source directory')
    parser.add_argument('--results-dir', default='results', help='Results output directory')
    
    # Redis configuration options
    parser.add_argument('--io-threads', type=int, help='Number of I/O threads (1-128)', default=4)
    parser.add_argument('--io-threads-do-reads', choices=['yes', 'no'], help='Enable I/O threads for reads', default='yes')
    parser.add_argument('--maxmemory', help='Maximum memory usage (e.g., 1gb, 512mb)', default='256gb')
    parser.add_argument('--hz', type=int, help='Background task frequency (1-500)', default=100)
    
    # Memtier benchmark parameters
    parser.add_argument('-c', '--clients', type=int, help='Number of clients per thread', default=50)
    parser.add_argument('-t', '--threads', type=int, help='Number of threads', default=4)
    parser.add_argument('-n', '--requests', type=int, help='Number of requests per client', default=10000000)
    parser.add_argument('-d', '--data-size', type=int, help='Data size in bytes', default=32)
    parser.add_argument('-P', '--pipeline', type=int, help='Pipeline requests', default=1)
    parser.add_argument('--ratio', help='SET:GET ratio (e.g., 1:10)', default='1:10')
    parser.add_argument('--key-pattern', help='Key pattern (R:R for random, S:S for sequential, G:G for gaussian)', default='R:R')
    parser.add_argument('--key-maximum', type=int, help='Maximum number of keys', default=10000000)
    
    args = parser.parse_args()
    
    # Build config options
    config_options = {}
    if args.io_threads:
        config_options['io_threads'] = args.io_threads
    if args.io_threads_do_reads:
        config_options['io_threads_do_reads'] = args.io_threads_do_reads
    if args.maxmemory:
        config_options['maxmemory'] = args.maxmemory
    if args.hz:
        config_options['hz'] = args.hz
    
    # Build benchmark parameters
    benchmark_params = {
        'clients': args.clients,
        'threads': args.threads,
        'requests': args.requests,
        'data_size': args.data_size,
        'pipeline': args.pipeline,
        'ratio': args.ratio,
        'key_pattern': args.key_pattern,
        'key_maximum': args.key_maximum
    }
    
    try:
        benchmark = MemtierBenchmark(redis_dir=args.redis_dir, results_dir=args.results_dir, config_options=config_options)
    except Exception as e:
        print(f"[ERROR] Failed to create benchmark instance: {e}")
        sys.exit(1)
    
    print("\nMemtier Benchmark Suite for Redis")
    print("=" * 60)
    
    try:
        results = benchmark.run_comprehensive_benchmark(**benchmark_params)
        
        if results:
            # Save detailed results
            results_file = benchmark.save_results(results)
            
            # Generate and display summary
            summary = benchmark.generate_summary(results)
            
            print("\n" + "=" * 60)
            print("BENCHMARK SUMMARY")
            print("=" * 60)
            print(f"Total tests: {summary['total_tests']}")
            print(f"Successful: {summary['successful_tests']}")
            print(f"Failed: {summary['failed_tests']}")
            print(f"Total duration: {summary['total_duration']:.2f} seconds")
            
            print("\nTest Results:")
            print("-" * 60)
            for test in summary.get('test_summary', []):
                status_symbol = "✓" if test['status'] == 'success' else "✗"
                print(f"{status_symbol} {test['test_name']:<20} {test['duration']:>6.2f}s", end="")
                
                metrics = test.get('metrics', {})
                if metrics:
                    if 'ops_per_second' in metrics:
                        print(f"  {metrics['ops_per_second']:>10,.0f} ops/sec", end="")
                    if 'avg_latency_ms' in metrics:
                        print(f"  {metrics['avg_latency_ms']:>6.3f}ms avg", end="")
                    if 'p99_latency_ms' in metrics:
                        print(f"  {metrics['p99_latency_ms']:>6.3f}ms p99", end="")
                print()
            
            print("=" * 60)
        else:
            print("[ERROR] Benchmark failed")
            sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Exception during benchmark: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Benchmark interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] Fatal error: {e}")
        sys.exit(1)
