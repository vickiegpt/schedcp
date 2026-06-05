#!/usr/bin/env python3
"""
CXL-Micro Scheduler Testing Script
Tests different schedulers with double_bandwidth to compare memory bandwidth performance.
"""

import os
import sys
import subprocess
import json
import time
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
import argparse
import re

# Add the scheduler module to the path (relative to repository root)
repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(repo_root))

from scheduler import SchedulerRunner, SchedulerBenchmark


class CXLMicroBenchmarkTester(SchedulerBenchmark):
    """
    Specialized class for testing schedulers with CXL-micro benchmarks.
    
    This class extends SchedulerBenchmark to provide CXL memory bandwidth-specific
    functionality including performance testing and result visualization.
    """
    
    def __init__(self, double_bandwidth_path: str, results_dir: str = "results",
                 scheduler_runner: SchedulerRunner = None):
        """
        Initialize the CXLMicroBenchmarkTester.
        
        Args:
            double_bandwidth_path: Path to double_bandwidth binary
            results_dir: Directory to store results
            scheduler_runner: SchedulerRunner instance to use
        """
        super().__init__(scheduler_runner)
        
        self.double_bandwidth_path = double_bandwidth_path
        self.results_dir = results_dir
        
        # Create results directory
        os.makedirs(self.results_dir, exist_ok=True)
        
        # Default test parameters
        self.test_params = {
            "array_size": 1073741824,  # 1GB default
            "iterations": 1,
            "threads": 128,
            "timeout": 300,
            "read_ratio": 0.5,  # 50% readers, 50% writers
            "duration": 1,
            "random": True,
        }
        
        # Environment setup
        self.env = os.environ.copy()
    
    def set_test_params(self, **kwargs):
        """
        Update test parameters.
        
        Args:
            **kwargs: Test parameters to update
        """
        self.test_params.update(kwargs)
    
    def _build_double_bandwidth_command(self) -> list:
        """Build double_bandwidth command with current parameters"""
        cmd = [
            self.double_bandwidth_path,
            "--buffer-size", str(self.test_params["array_size"]),
            "--threads", str(self.test_params["threads"]),
            "--duration", str(self.test_params["duration"]),  # Use fixed duration for consistent testing
            "--read-ratio", str(self.test_params["read_ratio"]),
            "--json"  # Use JSON output for easier parsing
        ]
        
        # Add access pattern flag
        if self.test_params.get("random", True):
            cmd.append("--random")
        else:
            cmd.append("--sequential")
        
        return cmd
    
    def _parse_bandwidth_output(self, output: str) -> dict:
        """Parse double_bandwidth JSON output to extract performance metrics"""
        try:
            # First try to parse as JSON
            try:
                metrics = json.loads(output.strip())
                # Rename some fields for compatibility with existing code
                if "total_bandwidth_mbps" in metrics:
                    metrics["bandwidth_mbps"] = metrics["total_bandwidth_mbps"]
                if "test_duration" in metrics:
                    metrics["execution_time"] = metrics["test_duration"]
                metrics["raw_output"] = output
                return metrics
            except json.JSONDecodeError:
                # Fall back to regex parsing for backward compatibility
                metrics = {}
                
                # Parse output for bandwidth measurements
                bandwidth_pattern = r"Total [Bb]andwidth[:\s]+(\d+\.?\d*)\s*(MB/s|GB/s)"
                time_pattern = r"Test duration[:\s]+(\d+\.?\d*)\s*(s|seconds|ms)"
                
                # Find bandwidth
                bandwidth_match = re.search(bandwidth_pattern, output, re.IGNORECASE)
                if bandwidth_match:
                    value = float(bandwidth_match.group(1))
                    unit = bandwidth_match.group(2).upper()
                    # Convert to MB/s for consistency
                    if "GB" in unit:
                        value *= 1024
                    metrics["bandwidth_mbps"] = value
                
                # Find time
                time_match = re.search(time_pattern, output, re.IGNORECASE)
                if time_match:
                    value = float(time_match.group(1))
                    unit = time_match.group(2).lower()
                    # Convert to seconds
                    if "ms" in unit:
                        value /= 1000
                    metrics["execution_time"] = value
                
                # If we couldn't parse specific metrics, try to extract any numbers
                if not metrics:
                    numbers = re.findall(r'\d+\.?\d*', output)
                    if numbers:
                        # Assume the largest number is bandwidth
                        metrics["bandwidth_mbps"] = max(float(n) for n in numbers)
                        metrics["parse_warning"] = "Generic number parsing used"
                
                metrics["raw_output"] = output
                return metrics
            
        except Exception as e:
            return {"error": str(e), "raw_output": output}
    
    def run_cxl_benchmark(self, scheduler_name: str = None) -> dict:
        """
        Run double_bandwidth with specified scheduler.
        
        Args:
            scheduler_name: Name of the scheduler to test (None for default)
            
        Returns:
            Dictionary containing benchmark results
        """
        print(f"Running double_bandwidth with scheduler: {scheduler_name or 'default'}")
        print(f"Parameters: buffer_size={self.test_params['array_size']}, "
              f"threads={self.test_params['threads']}, "
              f"read_ratio={self.test_params['read_ratio']}")
        
        # Build command
        cmd = self._build_double_bandwidth_command()
        timeout = self.test_params["timeout"]
        
        try:
            if scheduler_name:
                # Run with specific scheduler
                exit_code, stdout, stderr = self.runner.run_command_with_scheduler(
                    scheduler_name, cmd, timeout=timeout, env=self.env
                )
            else:
                # Run with default scheduler
                try:
                    result = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        timeout=timeout,
                        env=self.env
                    )
                    exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
                except subprocess.TimeoutExpired:
                    exit_code, stdout, stderr = -1, "", "Command timed out"
                except Exception as e:
                    exit_code, stdout, stderr = -1, "", str(e)
        
        except Exception as e:
            return {
                "scheduler": scheduler_name or "default",
                "error": str(e),
                "exit_code": -1
            }
        
        if exit_code != 0:
            print(f"Warning: double_bandwidth exited with code {exit_code}")
            print(f"stderr: {stderr}")
            return {
                "scheduler": scheduler_name or "default",
                "error": stderr or f"Exit code: {exit_code}",
                "exit_code": exit_code
            }
        
        # Parse results
        print(f"stdout: {stdout}")
        metrics = self._parse_bandwidth_output(stdout)
        metrics["scheduler"] = scheduler_name or "default"
        metrics["exit_code"] = exit_code
        metrics["array_size"] = self.test_params["array_size"]
        metrics["threads"] = self.test_params["threads"]
        metrics["read_ratio"] = self.test_params["read_ratio"]
        metrics["iterations"] = self.test_params["iterations"]
        
        return metrics
    
    def run_all_cxl_benchmarks(self, production_only: bool = True) -> dict:
        """
        Run CXL bandwidth tests for all schedulers.
        
        Args:
            production_only: Only test production-ready schedulers
            
        Returns:
            Dictionary mapping scheduler names to benchmark results
        """
        results = {}
        
        # Test default scheduler first
        print("Testing default scheduler...")
        results["default"] = self.run_cxl_benchmark()
        
        # Test each scheduler
        schedulers = self.runner.get_available_schedulers(production_only)
        for scheduler_name in schedulers:
            try:
                print(f"\nTesting scheduler: {scheduler_name}")
                results[scheduler_name] = self.run_cxl_benchmark(scheduler_name)
                
                # Save intermediate results
                self.save_results(results)
                
                # Brief pause between tests
                time.sleep(2)
                
            except Exception as e:
                print(f"Error testing scheduler {scheduler_name}: {e}")
                results[scheduler_name] = {
                    "scheduler": scheduler_name,
                    "error": str(e),
                    "bandwidth_mbps": 0
                }
        
        return results
    
    def save_results(self, results: dict):
        """Save results to JSON file"""
        # Generate filename with test parameters
        array_size_gb = self.test_params["array_size"] / (1024**3)
        threads = self.test_params["threads"]
        read_ratio = self.test_params["read_ratio"]
        access_pattern = "random" if self.test_params.get("random", True) else "seq"
        
        filename = f"cxl_scheduler_results_t{threads}_s{array_size_gb:.0f}gb_r{read_ratio:.2f}_{access_pattern}.json"
        results_file = os.path.join(self.results_dir, filename)
        
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {results_file}")
    
    def generate_performance_figures(self, results: dict):
        """Generate performance comparison figures"""
        
        # Extract data for plotting
        schedulers = []
        bandwidth_values = []
        
        for scheduler_name, result in results.items():
            if "error" in result:
                continue
            
            schedulers.append(scheduler_name)
            bandwidth_values.append(result.get("bandwidth_mbps", 0))
        
        if not schedulers:
            print("No valid results to plot")
            return
        
        # Create figure with single plot
        plt.figure(figsize=(12, 8))
        
        # Bandwidth Performance
        bars = plt.bar(schedulers, bandwidth_values, color='skyblue', alpha=0.8)
        plt.xlabel('Scheduler')
        plt.ylabel('Bandwidth (MB/s)')
        plt.title('CXL Memory Bandwidth Scheduler Performance Comparison')
        plt.xticks(rotation=45)
        plt.grid(True, alpha=0.3)
        
        # Add value labels on bars
        for bar in bars:
            height = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.0f}',
                    ha='center', va='bottom', fontsize=10)
        
        plt.tight_layout()
        
        # Save figure with test parameters in filename
        array_size_gb = self.test_params["array_size"] / (1024**3)
        threads = self.test_params["threads"]
        read_ratio = self.test_params["read_ratio"]
        access_pattern = "random" if self.test_params.get("random", True) else "seq"
        
        figure_filename = f"cxl_scheduler_performance_t{threads}_s{array_size_gb:.0f}gb_r{read_ratio:.2f}_{access_pattern}.png"
        figure_path = os.path.join(self.results_dir, figure_filename)
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        print(f"Performance figure saved to {figure_path}")
        
        # Print summary
        self.print_performance_summary(results)
    
    def print_performance_summary(self, results: dict):
        """Print performance summary"""
        print("\n" + "="*60)
        print("CXL MEMORY BANDWIDTH SCHEDULER PERFORMANCE SUMMARY")
        print("="*60)
        
        for scheduler_name, result in results.items():
            if "error" in result:
                print(f"\n{scheduler_name:15} ERROR: {result['error']}")
                continue
            
            print(f"\n{scheduler_name:15}")
            print(f"  Bandwidth:       {result.get('bandwidth_mbps', 0):8.1f} MB/s")
            if "execution_time" in result:
                print(f"  Execution Time:  {result.get('execution_time', 0):8.2f} seconds")
            print(f"  Array Size:      {result.get('array_size', 0) / (1024**3):8.2f} GB")
            print(f"  Threads:         {result.get('threads', 0):8d}")
            print(f"  Read Ratio:      {result.get('read_ratio', 0):8.2f}")
    
    def run_parameter_sweep_multi_schedulers(self, schedulers: list = None,
                                            thread_counts: list = None, 
                                            read_ratios: list = None,
                                            production_only: bool = False):
        """
        Run parameter sweep for multiple schedulers and generate comparison plots.
        
        Args:
            schedulers: List of scheduler names to test (None for all + default)
            thread_counts: List of thread counts to test
            read_ratios: List of read ratios to test (0.0-1.0)
            production_only: Only test production-ready schedulers if schedulers is None
        """
        thread_counts = thread_counts or [64]
        read_ratios = read_ratios or [0, 0.25, 0.35, 0.5, 0.75, 1]
        
        # Get schedulers to test
        if schedulers is None:
            schedulers = ['default'] + self.runner.get_available_schedulers(production_only)
        elif 'default' not in schedulers:
            schedulers = ['default'] + schedulers
                
        print(f"Running parameter sweep for schedulers: {schedulers}")
        print(f"Thread counts: {thread_counts}")
        print(f"Read ratios: {read_ratios}")
        print(f"Fixed array size: {self.test_params['array_size']/(1024**3):.1f} GB")
        
        all_results = []
        total_tests = len(schedulers) * len(thread_counts) * len(read_ratios)
        test_count = 0
        
        for scheduler_name in schedulers:
            print(f"\n{'='*50}")
            print(f"Testing scheduler: {scheduler_name}")
            print(f"{'='*50}")
            
            for threads in thread_counts:
                for read_ratio in read_ratios:
                    test_count += 1
                    print(f"\nTest {test_count}/{total_tests}: "
                          f"scheduler={scheduler_name}, threads={threads}, read_ratio={read_ratio:.2f}")
                    
                    # Update test parameters (preserve other settings like timeout, iterations)
                    self.set_test_params(
                        threads=threads, 
                        array_size=self.test_params["array_size"], 
                        read_ratio=read_ratio,
                        duration=self.test_params["duration"],
                        timeout=self.test_params["timeout"],
                        iterations=self.test_params["iterations"]
                    )
                    
                    # Run benchmark
                    scheduler_to_test = None if scheduler_name == 'default' else scheduler_name
                    result = self.run_cxl_benchmark(scheduler_to_test)
                    
                    if "error" not in result:
                        all_results.append({
                            'scheduler': scheduler_name,
                            'threads': threads,
                            'read_ratio': read_ratio,
                            'bandwidth_mbps': result.get('bandwidth_mbps', 0),
                            'execution_time': result.get('execution_time', 0),
                            'array_size_gb': self.test_params["array_size"] / (1024**3),
                            'duration': self.test_params["duration"]
                        })
                        print(f"  Bandwidth: {result.get('bandwidth_mbps', 0):.2f} MB/s")
                    else:
                        print(f"  Failed: {result['error']}")
                        all_results.append({
                            'scheduler': scheduler_name,
                            'threads': threads,
                            'read_ratio': read_ratio,
                            'bandwidth_mbps': 0,
                            'execution_time': 0,
                            'array_size_gb': self.test_params["array_size"] / (1024**3),
                            'duration': self.test_params["duration"]
                        })
                    
                    time.sleep(1)  # Brief pause between tests
        
        if all_results:
            # Save results
            df = pd.DataFrame(all_results)
            results_file = os.path.join(self.results_dir, "parameter_sweep_multi_schedulers.csv")
            df.to_csv(results_file, index=False)
            print(f"\nMulti-scheduler parameter sweep results saved to {results_file}")
            
            # Generate multi-scheduler comparison visualization
            self._generate_multi_scheduler_sweep_plot(df)
    
    def _generate_multi_scheduler_sweep_plot(self, df):
        """Generate parameter sweep visualization comparing multiple schedulers in larger subplots"""
        
        # Get unique values
        schedulers = sorted(df['scheduler'].unique())
        thread_counts = sorted(df['threads'].unique())
        read_ratios = sorted(df['read_ratio'].unique())
        
        # Calculate grid dimensions
        n_threads = len(thread_counts)
        n_ratios = len(read_ratios)
        
        # Create figure with larger subplots - similar to generate_performance_figures
        fig, axes = plt.subplots(n_ratios, n_threads, figsize=(8*n_threads, 6*n_ratios))
        fig.suptitle('Multi-Scheduler Parameter Sweep: Bandwidth Comparison', 
                     fontsize=20, y=0.98)
        
        # Ensure axes is always 2D array for consistent indexing
        if n_ratios == 1:
            axes = axes.reshape(1, -1)
        if n_threads == 1:
            axes = axes.reshape(-1, 1)
        
        # Color palette for different schedulers - use distinct colors
        colors = plt.cm.tab10(np.linspace(0, 1, len(schedulers)))
        scheduler_colors = {sched: colors[i] for i, sched in enumerate(schedulers)}
        
        # Create grouped bar plots for each configuration
        for i, read_ratio in enumerate(read_ratios):
            for j, threads in enumerate(thread_counts):
                ax = axes[i, j]
                
                # Get data for this specific configuration
                config_data = df[(df['threads'] == threads) & (df['read_ratio'] == read_ratio)]
                
                if not config_data.empty:
                    # Prepare data for grouped bars
                    bandwidths = []
                    scheduler_names = []
                    
                    for sched in schedulers:
                        sched_data = config_data[config_data['scheduler'] == sched]
                        if not sched_data.empty:
                            bandwidths.append(sched_data['bandwidth_mbps'].values[0])
                            scheduler_names.append(sched)
                        else:
                            bandwidths.append(0)
                            scheduler_names.append(sched)
                    
                    # Create bars - similar style to generate_performance_figures
                    bars = ax.bar(scheduler_names, bandwidths, 
                                 color=[scheduler_colors[s] for s in scheduler_names],
                                 alpha=0.8, edgecolor='black', linewidth=1)
                    
                    # Add value labels on bars
                    for bar in bars:
                        height = bar.get_height()
                        if height > 0:
                            ax.text(bar.get_x() + bar.get_width()/2., height,
                                   f'{height:.0f}',
                                   ha='center', va='bottom', fontsize=12, fontweight='bold')
                    
                    # Set title for each subplot
                    ax.set_title(f'Threads: {threads}, Read Ratio: {read_ratio:.2f}', 
                               fontsize=14, fontweight='bold', pad=20)
                    
                    # Set axis labels with larger fonts
                    ax.set_ylabel('Bandwidth (MB/s)', fontsize=12)
                    ax.set_xlabel('Scheduler', fontsize=12)
                    
                    # Rotate x-axis labels for better readability
                    ax.set_xticks(np.arange(len(scheduler_names)))
                    ax.set_xticklabels(scheduler_names, rotation=45, ha='right', fontsize=11)
                    
                    # Add grid
                    ax.grid(True, alpha=0.3, axis='y')
                    
                    # Set y-axis limits for consistency
                    max_bandwidth = df['bandwidth_mbps'].max()
                    ax.set_ylim(0, max_bandwidth * 1.2)
                    
                    # Improve tick formatting
                    ax.tick_params(axis='y', labelsize=11)
                    
                else:
                    # If no data, show empty plot with message
                    ax.text(0.5, 0.5, 'No Data Available', 
                           ha='center', va='center', transform=ax.transAxes,
                           fontsize=14, fontweight='bold')
                    ax.set_title(f'Threads: {threads}, Read Ratio: {read_ratio:.2f}', 
                               fontsize=14, fontweight='bold')
                    ax.set_xticks([])
                    ax.set_yticks([])
        
        plt.tight_layout()
        
        # Save figure
        figure_path = os.path.join(self.results_dir, "parameter_sweep_multi_schedulers.png")
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        print(f"Multi-scheduler parameter sweep figure saved to {figure_path}")

        pdf_path = os.path.join(self.results_dir, "parameter_sweep_multi_schedulers.pdf")
        plt.savefig(pdf_path, format='pdf', bbox_inches='tight')
        print(f"Multi-scheduler parameter sweep PDF saved to {pdf_path}")


def main():
    """Main function for CXL-micro scheduler testing"""
    parser = argparse.ArgumentParser(description="Test schedulers with CXL-micro double_bandwidth")
    parser.add_argument("--double-bandwidth-path", 
                       default=str(Path(__file__).resolve().parent / "double_bandwidth"),
                       help="Path to double_bandwidth binary")
    parser.add_argument("--results-dir", default="results", 
                       help="Directory to store results")
    parser.add_argument("--production-only", action="store_true", 
                       help="Test only production schedulers", default=False)
    parser.add_argument("--threads", type=int, default=256, 
                       help="Number of threads for testing")
    parser.add_argument("--array-size", type=int, default=16*1024*1024*1024, 
                       help="Array size in bytes (default 16GB)")
    parser.add_argument("--duration", type=int, default=10, 
                       help="Duration in seconds (default 1)")
    parser.add_argument("--iterations", type=int, default=1, 
                       help="Number of iterations per test")
    parser.add_argument("--read-ratio", type=float, default=0.5,
                       help="Ratio of readers (0.0-1.0, default: 0.5)")
    parser.add_argument("--timeout", type=int, default=30000, 
                       help="Timeout in seconds")
    parser.add_argument("--parameter-sweep", action="store_true",
                       help="Run parameter sweep comparing all schedulers")
    parser.add_argument("--scheduler", type=str, default=None,
                       help="Test specific scheduler only")
    parser.add_argument("--random", "-R", action="store_true", default=True,
                       help="Use random memory access pattern instead of sequential")
    
    args = parser.parse_args()
    
    # Create tester instance
    tester = CXLMicroBenchmarkTester(args.double_bandwidth_path, args.results_dir)
    
    # Validate read_ratio
    if args.read_ratio < 0.0 or args.read_ratio > 1.0:
        print("Error: read-ratio must be between 0.0 and 1.0")
        sys.exit(1)
    
    # Update test parameters
    tester.set_test_params(
        threads=args.threads,
        array_size=args.array_size,
        duration=args.duration,
        iterations=args.iterations,
        timeout=args.timeout,
        read_ratio=args.read_ratio,
        random=args.random
    )
    
    # Check if binary exists
    if not os.path.exists(args.double_bandwidth_path):
        print(f"Error: double_bandwidth not found at {args.double_bandwidth_path}")
        sys.exit(1)
    
    if args.parameter_sweep:
        print("Running multi-scheduler parameter sweep...")
        schedulers = None
        if args.scheduler:
            schedulers = [args.scheduler]
        tester.run_parameter_sweep_multi_schedulers(
            schedulers=schedulers,
            production_only=args.production_only
        )
    else:
        if args.scheduler:
            print(f"Testing scheduler: {args.scheduler}")
            result = tester.run_cxl_benchmark(args.scheduler)
            results = {args.scheduler: result}
        else:
            print("Starting CXL memory bandwidth scheduler performance tests...")
            results = tester.run_all_cxl_benchmarks(production_only=args.production_only)
        
        # Generate figures
        tester.generate_performance_figures(results)
    
    print("\nTesting complete!")


if __name__ == "__main__":
    main()
