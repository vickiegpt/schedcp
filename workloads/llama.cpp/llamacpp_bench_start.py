#!/usr/bin/env python3
"""
Llama.cpp Scheduler Testing Script
Tests different schedulers with llama_benchmark to compare AI workload performance.
"""

import os
import sys
import subprocess
import json
import time
import pandas as pd
import matplotlib.pyplot as plt
try:
    import seaborn as sns
except ImportError:
    sns = None
import numpy as np
from pathlib import Path
import argparse

# Add the scheduler module to the path (relative to repository root)
repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(repo_root))

from scheduler import SchedulerRunner, SchedulerBenchmark


class LlamaBenchmarkTester(SchedulerBenchmark):
    """
    Specialized class for testing schedulers with llama_benchmark.
    
    This class extends SchedulerBenchmark to provide llama.cpp-specific
    functionality including performance testing and result visualization.
    """
    
    def __init__(self, llama_bench_path: str, model_path: str, results_dir: str = "results",
                 scheduler_runner: SchedulerRunner = None):
        """
        Initialize the LlamaBenchmarkTester.
        
        Args:
            llama_bench_path: Path to llama-bench binary
            model_path: Path to the model file
            results_dir: Directory to store results
            scheduler_runner: SchedulerRunner instance to use
        """
        super().__init__(scheduler_runner)
        
        self.llama_bench_path = os.path.abspath(llama_bench_path)
        self.model_path = os.path.abspath(model_path)
        self.results_dir = results_dir
        
        # Create results directory
        os.makedirs(self.results_dir, exist_ok=True)
        
        # Default test parameters
        self.test_params = {
            "threads": 8,
            "batch_size": 512,
            "repetitions": 3,
            "timeout": 300,
            "n_samples": 50,
        }
        
        # Environment setup - change to the directory containing llama-bench
        # This ensures all shared libraries are found correctly
        self.build_dir = os.path.dirname(self.llama_bench_path)
        self.env = os.environ.copy()
        lib_path = self.build_dir
        if 'LD_LIBRARY_PATH' in self.env:
            self.env['LD_LIBRARY_PATH'] = f"{lib_path}:{self.env['LD_LIBRARY_PATH']}"
        else:
            self.env['LD_LIBRARY_PATH'] = lib_path
    
    def set_test_params(self, **kwargs):
        """
        Update test parameters.
        
        Args:
            **kwargs: Test parameters to update
        """
        self.test_params.update(kwargs)
    
    def _build_llama_bench_command(self) -> list:
        """Build llama-bench command with current parameters"""
        return [
            self.llama_bench_path,
            "-m", self.model_path,
            "-t", str(self.test_params["threads"]),
            "-b", str(self.test_params["batch_size"]),
            "-r", str(self.test_params["repetitions"]),
            "-n", str(self.test_params["n_samples"]),
            "-o", "json",
        ]
    
    def _parse_llama_output(self, output: str) -> dict:
        """Parse llama-bench output to extract performance metrics"""
        try:
            benchmark_result = json.loads(output)
            return self._extract_metrics(benchmark_result)
        except json.JSONDecodeError:
            return {"error": "Failed to parse JSON output"}
        except Exception as e:
            return {"error": str(e)}
    
    def _extract_metrics(self, benchmark_result):
        """Extract performance metrics from benchmark result."""
        if not benchmark_result:
            return {"error": "No benchmark result"}

        # llama-bench may return either a raw list or a dict containing "results"
        if isinstance(benchmark_result, dict):
            benchmark_result = benchmark_result.get("results", [])

        # At this point we expect a list of result objects
        if not isinstance(benchmark_result, list) or not benchmark_result:
            return {"error": "Invalid benchmark result format"}

        # Identify prompt-processing (PP) and text-generation (TG) runs
        pp_obj = next((r for r in benchmark_result if r.get("n_prompt", 0) > 0), None)
        tg_obj = next((r for r in benchmark_result if r.get("n_gen", 0) > 0), None)

        # Helper to safely extract average tokens / second ("avg_ts")
        def _tps(obj):
            return float(obj.get("avg_ts", 0)) if obj else 0.0

        # Helper to compute total time in seconds from avg_ns and token count
        def _time(obj, n_tokens_key):
            if not obj:
                return 0.0
            avg_ns = obj.get("avg_ns", 0)
            n_tokens = obj.get(n_tokens_key, 0)
            try:
                if avg_ns:
                    return avg_ns / 1e9
                avg_ts = obj.get("avg_ts", 0)
                return (n_tokens / avg_ts) if avg_ts else 0.0
            except Exception:
                return 0.0

        return {
            "pp_tps": _tps(pp_obj),
            "tg_tps": _tps(tg_obj),
            "pp_time": _time(pp_obj, "n_prompt"),
            "tg_time": _time(tg_obj, "n_gen"),
            "raw_output": benchmark_result
        }
    
    def run_llama_benchmark(self, scheduler_name: str = None) -> dict:
        """
        Run llama-bench with specified scheduler.
        
        Args:
            scheduler_name: Name of the scheduler to test (None for default)
            
        Returns:
            Dictionary containing benchmark results
        """
        print(f"Running llama-bench with scheduler: {scheduler_name or 'default'}")
        
        # Build llama-bench command
        cmd = self._build_llama_bench_command()
        timeout = self.test_params["timeout"]
        
        # Save current directory and change to build directory
        original_dir = os.getcwd()
        os.chdir(self.build_dir)
        
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
        finally:
            # Restore original directory
            os.chdir(original_dir)
        
        if exit_code != 0:
            print(f"Warning: llama-bench exited with code {exit_code}")
            print(f"stderr: {stderr}")
            return {
                "scheduler": scheduler_name or "default",
                "error": stderr or f"Exit code: {exit_code}",
                "exit_code": exit_code
            }
        
        # Parse results
        metrics = self._parse_llama_output(stdout)
        metrics["scheduler"] = scheduler_name or "default"
        metrics["exit_code"] = exit_code
        
        return metrics
    
    def run_all_llama_benchmarks(self, production_only: bool = True,
                                  schedulers: list = None,
                                  include_default: bool = True) -> dict:
        """
        Run llama-bench tests for all schedulers.
        
        Args:
            production_only: Only test production-ready schedulers
            
        Returns:
            Dictionary mapping scheduler names to benchmark results
        """
        results = {}
        
        # Test default scheduler first
        if include_default:
            print("Testing default scheduler...")
            results["default"] = self.run_llama_benchmark()
            self.save_results(results)

        # Test each scheduler
        schedulers = schedulers or self.runner.get_available_schedulers(production_only)
        for scheduler_name in schedulers:
            try:
                print(f"Testing scheduler: {scheduler_name}")
                results[scheduler_name] = self.run_llama_benchmark(scheduler_name)
                
                # Save intermediate results
                self.save_results(results)
                
                # Brief pause between tests
                time.sleep(2)
                
            except Exception as e:
                print(f"Error testing scheduler {scheduler_name}: {e}")
                results[scheduler_name] = {
                    "scheduler": scheduler_name,
                    "error": str(e),
                    "pp_tps": 0,
                    "tg_tps": 0
                }
        
        return results
    
    def save_results(self, results: dict):
        """Save results to JSON file"""
        results_file = os.path.join(self.results_dir, "llama_scheduler_results.json")
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {results_file}")
    
    def generate_performance_figures(self, results: dict):
        """Generate performance comparison figures"""
        
        # Extract data for plotting
        schedulers = []
        pp_tps = []
        tg_tps = []
        pp_time = []
        tg_time = []
        
        for scheduler_name, result in results.items():
            if "error" in result:
                continue
            
            schedulers.append(scheduler_name)
            pp_tps.append(result.get("pp_tps", 0))
            tg_tps.append(result.get("tg_tps", 0))
            pp_time.append(result.get("pp_time", 0))
            tg_time.append(result.get("tg_time", 0))
        
        if not schedulers:
            print("No valid results to plot")
            return
        
        # Create figure with subplots
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
        fig.suptitle('Scheduler Performance Comparison with Llama.cpp', fontsize=16)
        
        # Plot 1: Prompt Processing Performance
        ax1.bar(schedulers, pp_tps, color='skyblue', alpha=0.8)
        ax1.set_xlabel('Scheduler')
        ax1.set_ylabel('Prompt Processing (tokens/sec)')
        ax1.set_title('Prompt Processing Performance')
        ax1.tick_params(axis='x', rotation=45)
        ax1.grid(True, alpha=0.3)
        
        # Plot 2: Text Generation Performance
        ax2.bar(schedulers, tg_tps, color='lightgreen', alpha=0.8)
        ax2.set_xlabel('Scheduler')
        ax2.set_ylabel('Text Generation (tokens/sec)')
        ax2.set_title('Text Generation Performance')
        ax2.tick_params(axis='x', rotation=45)
        ax2.grid(True, alpha=0.3)
        
        # Plot 3: Performance Comparison
        x = np.arange(len(schedulers))
        width = 0.35
        
        ax3.bar(x - width/2, pp_tps, width, label='Prompt Processing', alpha=0.8)
        ax3.bar(x + width/2, tg_tps, width, label='Text Generation', alpha=0.8)
        
        ax3.set_xlabel('Scheduler')
        ax3.set_ylabel('Performance (tokens/sec)')
        ax3.set_title('PP vs TG Performance')
        ax3.set_xticks(x)
        ax3.set_xticklabels(schedulers, rotation=45, ha='right')
        ax3.legend()
        ax3.grid(True, alpha=0.3)
        
        # Plot 4: Performance Summary (Normalized)
        if len(schedulers) > 1:
            # Normalize metrics (higher is better)
            norm_pp = np.array(pp_tps) / max(pp_tps) if max(pp_tps) > 0 else np.zeros(len(pp_tps))
            norm_tg = np.array(tg_tps) / max(tg_tps) if max(tg_tps) > 0 else np.zeros(len(tg_tps))
            
            # Combined score (weighted average, TG more important)
            combined_score = (norm_pp * 0.3 + norm_tg * 0.7)
            
            bars = ax4.bar(schedulers, combined_score, color='orange', alpha=0.8)
            ax4.set_xlabel('Scheduler')
            ax4.set_ylabel('Combined Performance Score')
            ax4.set_title('Overall Performance Score\n(Higher is Better)')
            ax4.tick_params(axis='x', rotation=45)
            ax4.grid(True, alpha=0.3)
            
            # Add value labels on bars
            for bar in bars:
                height = bar.get_height()
                ax4.text(bar.get_x() + bar.get_width()/2., height,
                        f'{height:.2f}',
                        ha='center', va='bottom', fontsize=8)
        
        plt.tight_layout()
        
        # Save figure
        figure_path = os.path.join(self.results_dir, "llama_scheduler_performance.png")
        pdf_path = os.path.join(self.results_dir, "llama_scheduler_performance.pdf")
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        plt.savefig(pdf_path, format="pdf", bbox_inches='tight')
        plt.close(fig)
        print(f"Performance figure saved to {figure_path}")
        print(f"Performance PDF saved to {pdf_path}")
        
        # Print summary
        self.print_performance_summary(results)
    
    def print_performance_summary(self, results: dict):
        """Print performance summary"""
        print("\n" + "="*60)
        print("LLAMA.CPP SCHEDULER PERFORMANCE SUMMARY")
        print("="*60)
        
        for scheduler_name, result in results.items():
            if "error" in result:
                print(f"\n{scheduler_name:15} ERROR: {result['error']}")
                continue
            
            print(f"\n{scheduler_name:15}")
            print(f"  PP Performance:  {result.get('pp_tps', 0):8.1f} tokens/sec")
            print(f"  TG Performance:  {result.get('tg_tps', 0):8.1f} tokens/sec")
            print(f"  PP Time:         {result.get('pp_time', 0):8.2f} seconds")
            print(f"  TG Time:         {result.get('tg_time', 0):8.2f} seconds")
    
    def run_parameter_sweep(self, scheduler_name: str = None, 
                           thread_counts: list = None, batch_sizes: list = None):
        """
        Run parameter sweep for a specific scheduler.
        
        Args:
            scheduler_name: Name of the scheduler to test
            thread_counts: List of thread counts to test
            batch_sizes: List of batch sizes to test
        """
        thread_counts = thread_counts or [1, 2, 4, 8, 16]
        batch_sizes = batch_sizes or [128, 256, 512, 1024]
        
        print(f"Running parameter sweep for scheduler: {scheduler_name or 'default'}")
        print(f"Thread counts: {thread_counts}")
        print(f"Batch sizes: {batch_sizes}")
        
        results = []
        total_tests = len(thread_counts) * len(batch_sizes)
        test_count = 0
        
        for threads in thread_counts:
            for batch_size in batch_sizes:
                test_count += 1
                print(f"Test {test_count}/{total_tests}: threads={threads}, batch_size={batch_size}")
                
                # Update test parameters
                self.set_test_params(threads=threads, batch_size=batch_size)
                
                # Run benchmark
                result = self.run_llama_benchmark(scheduler_name)
                
                if "error" not in result:
                    results.append({
                        'scheduler': scheduler_name or 'default',
                        'threads': threads,
                        'batch_size': batch_size,
                        'pp_tps': result.get('pp_tps', 0),
                        'tg_tps': result.get('tg_tps', 0),
                        'pp_time': result.get('pp_time', 0),
                        'tg_time': result.get('tg_time', 0)
                    })
                    print(f"  PP: {result.get('pp_tps', 0):.2f} tps, TG: {result.get('tg_tps', 0):.2f} tps")
                else:
                    print(f"  Failed: {result['error']}")
                
                time.sleep(1)  # Brief pause between tests
        
        if results:
            # Save results
            df = pd.DataFrame(results)
            results_file = os.path.join(self.results_dir, f"parameter_sweep_{scheduler_name or 'default'}.csv")
            df.to_csv(results_file, index=False)
            print(f"Parameter sweep results saved to {results_file}")
            
            # Generate parameter sweep visualization
            self._generate_parameter_sweep_plot(df, scheduler_name or 'default')
    
    def _generate_parameter_sweep_plot(self, df, scheduler_name):
        """Generate parameter sweep visualization"""
        fig, axes = plt.subplots(2, 2, figsize=(15, 12))
        fig.suptitle(f'Parameter Sweep Results for {scheduler_name}', fontsize=16)
        
        # 1. Heatmap for TG performance
        tg_pivot = df.pivot(index='threads', columns='batch_size', values='tg_tps')
        if sns:
            sns.heatmap(tg_pivot, annot=True, fmt='.1f', cmap='viridis', ax=axes[0,0])
        else:
            axes[0,0].imshow(tg_pivot.values, aspect='auto')
        axes[0,0].set_title('Text Generation Performance (tokens/sec)')
        axes[0,0].set_xlabel('Batch Size')
        axes[0,0].set_ylabel('Thread Count')
        
        # 2. Heatmap for PP performance
        pp_pivot = df.pivot(index='threads', columns='batch_size', values='pp_tps')
        if sns:
            sns.heatmap(pp_pivot, annot=True, fmt='.1f', cmap='plasma', ax=axes[0,1])
        else:
            axes[0,1].imshow(pp_pivot.values, aspect='auto')
        axes[0,1].set_title('Prompt Processing Performance (tokens/sec)')
        axes[0,1].set_xlabel('Batch Size')
        axes[0,1].set_ylabel('Thread Count')
        
        # 3. Line plot for different thread counts
        batch_sizes = sorted(df['batch_size'].unique())
        for batch_size in batch_sizes:
            subset = df[df['batch_size'] == batch_size]
            axes[1,0].plot(subset['threads'], subset['tg_tps'], marker='o', label=f'Batch {batch_size}')
        axes[1,0].set_xlabel('Thread Count')
        axes[1,0].set_ylabel('Text Generation (tokens/sec)')
        axes[1,0].set_title('TG Performance vs Thread Count')
        axes[1,0].legend()
        axes[1,0].grid(True, alpha=0.3)
        
        # 4. Line plot for different batch sizes
        thread_counts = sorted(df['threads'].unique())
        for threads in thread_counts:
            subset = df[df['threads'] == threads]
            axes[1,1].plot(subset['batch_size'], subset['tg_tps'], marker='s', label=f'{threads} threads')
        axes[1,1].set_xlabel('Batch Size')
        axes[1,1].set_ylabel('Text Generation (tokens/sec)')
        axes[1,1].set_title('TG Performance vs Batch Size')
        axes[1,1].legend()
        axes[1,1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        
        # Save figure
        figure_path = os.path.join(self.results_dir, f"parameter_sweep_{scheduler_name}.png")
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        print(f"Parameter sweep figure saved to {figure_path}")


def main():

    # get the abs path opf current file
    current_file = os.path.abspath(__file__)
    current_dir = os.path.dirname(current_file)
    print(f"Current file: {current_file}")
    print(f"Current dir: {current_dir}")


    """Main function for llama scheduler testing"""
    parser = argparse.ArgumentParser(description="Test schedulers with llama-bench")
    parser.add_argument("--llama-bench-path",
                       default=os.path.join(current_dir, "build/bin/llama-bench"),
                       help="Path to llama-bench binary")
    parser.add_argument("--model-path",
                       default=os.path.join(current_dir, "models/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"),
                       help="Path to model file")
    parser.add_argument("--results-dir", default="results", 
                       help="Directory to store results")
    parser.add_argument("--production-only", action="store_true", 
                       help="Test only production schedulers")
    parser.add_argument("--threads", type=int, default=172, 
                       help="Number of threads for testing")
    parser.add_argument("--batch-size", type=int, default=4, 
                       help="Batch size for testing")
    parser.add_argument("--repetitions", type=int, default=1, 
                       help="Number of repetitions per test")
    parser.add_argument("--timeout", type=int, default=30000, 
                       help="Timeout in seconds")
    parser.add_argument("--parameter-sweep", action="store_true",
                       help="Run parameter sweep for each scheduler")
    parser.add_argument("--scheduler", type=str, default=None,
                       help="Test specific scheduler only")
    parser.add_argument("--schedulers", nargs="+", default=None,
                       help="Test an explicit list of schedulers after default")
    parser.add_argument("--skip-default", action="store_true",
                       help="Do not run the default Linux scheduler baseline")
    parser.add_argument("--default-only", action="store_true",
                       help="Run only the default Linux scheduler baseline")
    parser.add_argument("--n-samples", type=int, default=50,
                       help="Number of generated tokens for llama-bench")
    
    args = parser.parse_args()
    
    # Create tester instance
    tester = LlamaBenchmarkTester(args.llama_bench_path, args.model_path, args.results_dir)
    
    # Update test parameters
    tester.set_test_params(
        threads=args.threads,
        batch_size=args.batch_size,
        repetitions=args.repetitions,
        timeout=args.timeout,
        n_samples=args.n_samples
    )
    
    # Check if files exist
    if not os.path.exists(args.llama_bench_path):
        print(f"Error: llama-bench not found at {args.llama_bench_path}")
        sys.exit(1)
    
    if not os.path.exists(args.model_path):
        print(f"Error: Model file not found at {args.model_path}")
        sys.exit(1)
    
    if args.parameter_sweep:
        if args.scheduler:
            print(f"Running parameter sweep for scheduler: {args.scheduler}")
            tester.run_parameter_sweep(args.scheduler)
        else:
            print("Running parameter sweep for all schedulers...")
            # Run parameter sweep for default scheduler
            tester.run_parameter_sweep()
            
            # Run parameter sweep for each scheduler
            schedulers = tester.runner.get_available_schedulers(args.production_only)
            for scheduler_name in schedulers:
                try:
                    tester.run_parameter_sweep(scheduler_name)
                except Exception as e:
                    print(f"Error in parameter sweep for {scheduler_name}: {e}")
    else:
        if args.default_only:
            print("Testing default scheduler only")
            results = {"default": tester.run_llama_benchmark()}
        elif args.scheduler:
            print(f"Testing scheduler: {args.scheduler}")
            result = tester.run_llama_benchmark(args.scheduler)
            results = {args.scheduler: result}
        else:
            print("Starting llama scheduler performance tests...")
            results = tester.run_all_llama_benchmarks(
                production_only=args.production_only,
                schedulers=args.schedulers,
                include_default=not args.skip_default,
            )
        
        # Generate figures
        tester.generate_performance_figures(results)
    
    print("\nTesting complete!")


if __name__ == "__main__":
    main()
