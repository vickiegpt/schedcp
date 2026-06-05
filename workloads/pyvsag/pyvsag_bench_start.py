#!/usr/bin/env python3
"""
PyVSAG ANN Scheduler Testing Script
Tests different schedulers with PyVSAG (VSAG Python bindings) to compare vector search performance.
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
import traceback

# Add the scheduler module to the path
repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(repo_root / 'scheduler'))
sys.path.append(str(repo_root / 'include'))

from scheduler_runner import SchedulerRunner, SchedulerBenchmark
from schedcp_slug_marker import (
    slug_mark_balanced_bb,
    slug_mark_read_bb,
    slug_mark_write_bb,
)

# Try to import pyvsag and numpy
try:
    import pyvsag
    import numpy as np
    PYVSAG_AVAILABLE = True
except ImportError as e:
    PYVSAG_AVAILABLE = False
    IMPORT_ERROR = str(e)


class PyVSAGBenchmarkTester(SchedulerBenchmark):
    """
    Specialized class for testing schedulers with PyVSAG ANN searches.
    
    This class extends SchedulerBenchmark to provide VSAG vector search-specific
    functionality including index building, search performance testing and result visualization.
    """
    
    def __init__(self, results_dir: str = "results",
                 scheduler_runner: SchedulerRunner = None):
        """
        Initialize the PyVSAGBenchmarkTester.
        
        Args:
            results_dir: Directory to store results
            scheduler_runner: SchedulerRunner instance to use
        """
        super().__init__(scheduler_runner)
        
        self.results_dir = results_dir
        
        # Create results directory
        os.makedirs(self.results_dir, exist_ok=True)
        
        # Default test parameters
        self.test_params = {
            "dim": 128,
            "num_elements": 50000,
            "num_queries": 1000,
            "k": 10,
            "max_degree": 16,
            "ef_construction": 200,
            "ef_search": 100,
            "timeout": 300,
            "metric_type": "l2",
            "index_type": "hnsw",
        }
    
    def set_test_params(self, **kwargs):
        """
        Update test parameters.
        
        Args:
            **kwargs: Test parameters to update
        """
        self.test_params.update(kwargs)
    
    def _generate_test_data(self):
        """Generate test data for the benchmark"""
        dim = self.test_params["dim"]
        num_elements = self.test_params["num_elements"]
        num_queries = self.test_params["num_queries"]
        
        # Generate random data for index
        np.random.seed(42)  # For reproducibility
        data = np.random.random((num_elements, dim)).astype(np.float32)
        ids = list(range(num_elements))
        
        # Generate random queries
        queries = np.random.random((num_queries, dim)).astype(np.float32)
        
        return data, ids, queries
    
    def _build_index(self, data, ids):
        """Build VSAG index with given data"""
        dim = self.test_params["dim"]
        num_elements = self.test_params["num_elements"]
        
        # Create index parameters
        index_params = json.dumps({
            "dtype": "float32",
            "metric_type": self.test_params["metric_type"],
            "dim": dim,
            "hnsw": {
                "max_degree": self.test_params["max_degree"],
                "ef_construction": self.test_params["ef_construction"]
            }
        })
        
        # Create and build index
        index = pyvsag.Index(self.test_params["index_type"], index_params)
        
        build_start = time.time()
        slug_mark_write_bb()
        index.build(vectors=data, ids=ids, num_elements=num_elements, dim=dim)
        slug_mark_balanced_bb()
        build_time = time.time() - build_start
        
        return index, build_time
    
    def _run_search_benchmark(self, index, queries):
        """Run search benchmark and return performance metrics"""
        k = self.test_params["k"]
        search_params = json.dumps({
            "hnsw": {"ef_search": self.test_params["ef_search"]}
        })
        
        search_times = []
        total_results = 0
        slug_mark_read_bb()
        
        # Warmup
        for i in range(min(10, len(queries))):
            index.knn_search(vector=queries[i], k=k, parameters=search_params)
        
        # Actual benchmark
        start_time = time.time()
        for query in queries:
            query_start = time.time()
            ids, distances = index.knn_search(vector=query, k=k, parameters=search_params)
            query_time = time.time() - query_start
            
            search_times.append(query_time)
            total_results += len(ids)
        
        total_time = time.time() - start_time
        
        # Calculate metrics
        avg_query_time = np.mean(search_times)
        p95_query_time = np.percentile(search_times, 95)
        p99_query_time = np.percentile(search_times, 99)
        qps = len(queries) / total_time
        
        return {
            "avg_query_time_ms": avg_query_time * 1000,
            "p95_query_time_ms": p95_query_time * 1000,
            "p99_query_time_ms": p99_query_time * 1000,
            "total_search_time": total_time,
            "qps": qps,
            "num_queries": len(queries),
            "total_results": total_results
        }
    
    def _calculate_recall(self, index, data, ids, queries, ground_truth=None):
        """Calculate recall for the index (simplified version)"""
        k = self.test_params["k"]
        search_params = json.dumps({
            "hnsw": {"ef_search": self.test_params["ef_search"]}
        })
        slug_mark_read_bb()
        
        if ground_truth is None:
            # Simple recall test: search for vectors that are in the index
            correct = 0
            test_size = min(100, len(queries), len(data))
            
            for i in range(test_size):
                # Use actual data point as query to ensure we can find it
                query_vector = data[i]
                found_ids, _ = index.knn_search(vector=query_vector, k=k, parameters=search_params)
                if ids[i] in found_ids:
                    correct += 1
            
            recall = correct / test_size
        else:
            # Use provided ground truth
            correct = 0
            for i, query in enumerate(queries):
                found_ids, _ = index.knn_search(vector=query, k=k, parameters=search_params)
                if i < len(ground_truth):
                    true_neighbors = set(ground_truth[i][:k])
                    found_neighbors = set(found_ids)
                    intersection = len(true_neighbors.intersection(found_neighbors))
                    correct += intersection / k
            
            recall = correct / len(queries)
        
        return recall
    
    def run_pyvsag_benchmark(self, scheduler_name: str = None) -> dict:
        """
        Run PyVSAG benchmark with specified scheduler.
        
        Args:
            scheduler_name: Name of the scheduler to test (None for default)
            
        Returns:
            Dictionary containing benchmark results
        """
        if not PYVSAG_AVAILABLE:
            return {
                "scheduler": scheduler_name or "default",
                "error": f"PyVSAG not available: {IMPORT_ERROR}",
                "exit_code": -1
            }
        
        print(f"Running PyVSAG benchmark with scheduler: {scheduler_name or 'default'}")
        print(f"Parameters: dim={self.test_params['dim']}, "
              f"num_elements={self.test_params['num_elements']}, "
              f"num_queries={self.test_params['num_queries']}, "
              f"k={self.test_params['k']}")
        
        try:
            # Create a benchmark function that we'll run with the scheduler
            def run_benchmark():
                # Generate test data
                data, ids, queries = self._generate_test_data()
                
                # Build index
                index, build_time = self._build_index(data, ids)
                
                # Run search benchmark
                search_metrics = self._run_search_benchmark(index, queries)
                
                # Calculate recall
                recall = self._calculate_recall(index, data, ids, queries)
                
                return {
                    "build_time": build_time,
                    "recall": recall,
                    **search_metrics
                }
            
            if scheduler_name:
                # For scheduler tests, we need to run the benchmark in a subprocess
                # to ensure the scheduler affects the entire Python process
                result = self._run_with_scheduler(scheduler_name, run_benchmark)
            else:
                # Run directly for default scheduler
                result = run_benchmark()
            
            result["scheduler"] = scheduler_name or "default"
            result["exit_code"] = 0
            
            return result
            
        except Exception as e:
            print(f"Error in PyVSAG benchmark: {e}")
            traceback.print_exc()
            return {
                "scheduler": scheduler_name or "default",
                "error": str(e),
                "exit_code": -1
            }
    
    def _run_with_scheduler(self, scheduler_name: str, benchmark_func):
        """Run benchmark function with a specific scheduler using subprocess"""
        
        # Create a temporary script that runs the benchmark
        script_content = f'''
import sys
import os
import json
import traceback
import time

# Add the scheduler module to the path
sys.path.append('{os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scheduler"))}')
sys.path.append('{str(repo_root / "include")}')

try:
    import pyvsag
    import numpy as np
    from schedcp_slug_marker import slug_mark_balanced_bb, slug_mark_read_bb, slug_mark_write_bb
    
    # Import the class directly by recreating it here
    from scheduler_runner import SchedulerRunner, SchedulerBenchmark
    
    class PyVSAGBenchmarkTester(SchedulerBenchmark):
        def __init__(self, results_dir="results", scheduler_runner=None):
            super().__init__(scheduler_runner)
            self.results_dir = results_dir
            os.makedirs(self.results_dir, exist_ok=True)
            self.test_params = {json.dumps(self.test_params)}
        
        def _generate_test_data(self):
            dim = self.test_params["dim"]
            num_elements = self.test_params["num_elements"]
            num_queries = self.test_params["num_queries"]
            
            np.random.seed(42)
            data = np.random.random((num_elements, dim)).astype(np.float32)
            ids = list(range(num_elements))
            queries = np.random.random((num_queries, dim)).astype(np.float32)
            
            return data, ids, queries
        
        def _build_index(self, data, ids):
            dim = self.test_params["dim"]
            num_elements = self.test_params["num_elements"]
            
            index_params = json.dumps({{
                "dtype": "float32",
                "metric_type": self.test_params["metric_type"],
                "dim": dim,
                "hnsw": {{
                    "max_degree": self.test_params["max_degree"],
                    "ef_construction": self.test_params["ef_construction"]
                }}
            }})
            
            index = pyvsag.Index(self.test_params["index_type"], index_params)
            
            build_start = time.time()
            slug_mark_write_bb()
            index.build(vectors=data, ids=ids, num_elements=num_elements, dim=dim)
            slug_mark_balanced_bb()
            build_time = time.time() - build_start
            
            return index, build_time
        
        def _run_search_benchmark(self, index, queries):
            k = self.test_params["k"]
            search_params = json.dumps({{
                "hnsw": {{"ef_search": self.test_params["ef_search"]}}
            }})
            
            search_times = []
            total_results = 0
            slug_mark_read_bb()
            
            # Warmup
            for i in range(min(10, len(queries))):
                index.knn_search(vector=queries[i], k=k, parameters=search_params)
            
            # Actual benchmark
            start_time = time.time()
            for query in queries:
                query_start = time.time()
                ids, distances = index.knn_search(vector=query, k=k, parameters=search_params)
                query_time = time.time() - query_start
                
                search_times.append(query_time)
                total_results += len(ids)
            
            total_time = time.time() - start_time
            
            # Calculate metrics
            avg_query_time = np.mean(search_times)
            p95_query_time = np.percentile(search_times, 95)
            p99_query_time = np.percentile(search_times, 99)
            qps = len(queries) / total_time
            
            return {{
                "avg_query_time_ms": avg_query_time * 1000,
                "p95_query_time_ms": p95_query_time * 1000,
                "p99_query_time_ms": p99_query_time * 1000,
                "total_search_time": total_time,
                "qps": qps,
                "num_queries": len(queries),
                "total_results": total_results
            }}
        
        def _calculate_recall(self, index, data, ids, queries, ground_truth=None):
            k = self.test_params["k"]
            search_params = json.dumps({{
                "hnsw": {{"ef_search": self.test_params["ef_search"]}}
            }})
            slug_mark_read_bb()
            
            if ground_truth is None:
                # Simple recall test: search for vectors that are in the index
                correct = 0
                test_size = min(100, len(queries), len(data))
                
                for i in range(test_size):
                    # Use actual data point as query to ensure we can find it
                    query_vector = data[i]
                    found_ids, _ = index.knn_search(vector=query_vector, k=k, parameters=search_params)
                    if ids[i] in found_ids:
                        correct += 1
                
                recall = correct / test_size
            else:
                # Use provided ground truth
                correct = 0
                for i, query in enumerate(queries):
                    found_ids, _ = index.knn_search(vector=query, k=k, parameters=search_params)
                    if i < len(ground_truth):
                        true_neighbors = set(ground_truth[i][:k])
                        found_neighbors = set(found_ids)
                        intersection = len(true_neighbors.intersection(found_neighbors))
                        correct += intersection / k
                
                recall = correct / len(queries)
            
            return recall
    
    # Create tester with same parameters
    tester = PyVSAGBenchmarkTester()
    
    # Generate test data
    data, ids, queries = tester._generate_test_data()
    
    # Build index
    index, build_time = tester._build_index(data, ids)
    
    # Run search benchmark
    search_metrics = tester._run_search_benchmark(index, queries)
    
    # Calculate recall
    recall = tester._calculate_recall(index, data, ids, queries)
    
    result = {{
        "build_time": build_time,
        "recall": recall,
        **search_metrics
    }}
    
    print(json.dumps(result))
    
except Exception as e:
    print(json.dumps({{"error": str(e), "traceback": traceback.format_exc()}}))
'''
        
        # Write script to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            # Run with scheduler
            cmd = [sys.executable, script_path]
            timeout = self.test_params["timeout"]
            
            exit_code, stdout, stderr = self.runner.run_command_with_scheduler(
                scheduler_name, cmd, timeout=timeout
            )
            
            if exit_code != 0:
                return {
                    "error": stderr or f"Exit code: {exit_code}",
                    "exit_code": exit_code
                }
            
            # Parse JSON result
            try:
                # Extract JSON from output (might have log messages before it)
                lines = stdout.strip().split('\n')
                json_str = None
                for line in lines:
                    if line.startswith('{'):
                        json_str = line
                        break
                
                if json_str:
                    result = json.loads(json_str)
                    if "error" in result:
                        return result
                    return result
                else:
                    return {
                        "error": f"No JSON found in output: {stdout}",
                        "raw_output": stdout
                    }
            except json.JSONDecodeError:
                return {
                    "error": f"Failed to parse output: {stdout}",
                    "raw_output": stdout
                }
                
        finally:
            # Clean up temporary file
            try:
                os.unlink(script_path)
            except:
                pass
    
    def run_all_pyvsag_benchmarks(self, production_only: bool = True,
                                  scheduler_names: list = None) -> dict:
        """
        Run PyVSAG benchmarks for all schedulers.
        
        Args:
            production_only: Only test production-ready schedulers
            scheduler_names: Optional explicit scheduler list to test
            
        Returns:
            Dictionary mapping scheduler names to benchmark results
        """
        results = {}
        
        # Test default scheduler first
        print("Testing default scheduler...")
        results["default"] = self.run_pyvsag_benchmark()
        
        # Test each scheduler
        schedulers = scheduler_names
        if schedulers is None:
            schedulers = self.runner.get_available_schedulers(production_only)

        for scheduler_name in schedulers:
            try:
                print(f"\nTesting scheduler: {scheduler_name}")
                results[scheduler_name] = self.run_pyvsag_benchmark(scheduler_name)
                
                # Save intermediate results
                self.save_results(results)
                
                # Brief pause between tests
                time.sleep(2)
                
            except Exception as e:
                print(f"Error testing scheduler {scheduler_name}: {e}")
                results[scheduler_name] = {
                    "scheduler": scheduler_name,
                    "error": str(e),
                    "qps": 0,
                    "recall": 0
                }
        
        return results
    
    def save_results(self, results: dict):
        """Save results to JSON file"""
        results_file = os.path.join(self.results_dir, "pyvsag_scheduler_results.json")
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {results_file}")
    
    def generate_performance_figures(self, results: dict):
        """Generate performance comparison figures"""
        
        # Extract data for plotting
        schedulers = []
        qps_values = []
        recall_values = []
        avg_latency_values = []
        p95_latency_values = []
        
        for scheduler_name, result in results.items():
            if "error" in result:
                continue
            
            schedulers.append(scheduler_name)
            qps_values.append(result.get("qps", 0))
            recall_values.append(result.get("recall", 0))
            avg_latency_values.append(result.get("avg_query_time_ms", 0))
            p95_latency_values.append(result.get("p95_query_time_ms", 0))
        
        if not schedulers:
            print("No valid results to plot")
            return
        
        # Create figure with subplots
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
        fig.suptitle('PyVSAG ANN Search Scheduler Performance Comparison', fontsize=16)
        
        # Plot 1: Queries Per Second
        bars1 = ax1.bar(schedulers, qps_values, color='skyblue', alpha=0.8)
        ax1.set_xlabel('Scheduler')
        ax1.set_ylabel('Queries Per Second (QPS)')
        ax1.set_title('Search Throughput Performance')
        ax1.tick_params(axis='x', rotation=45)
        ax1.grid(True, alpha=0.3)
        
        # Add value labels on bars
        for bar in bars1:
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.1f}',
                    ha='center', va='bottom', fontsize=8)
        
        # Plot 2: Recall
        bars2 = ax2.bar(schedulers, recall_values, color='lightgreen', alpha=0.8)
        ax2.set_xlabel('Scheduler')
        ax2.set_ylabel('Recall')
        ax2.set_title('Search Accuracy (Recall)')
        ax2.tick_params(axis='x', rotation=45)
        ax2.grid(True, alpha=0.3)
        ax2.set_ylim(0, 1.0)
        
        # Add value labels on bars
        for bar in bars2:
            height = bar.get_height()
            ax2.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}',
                    ha='center', va='bottom', fontsize=8)
        
        # Plot 3: Average Latency
        bars3 = ax3.bar(schedulers, avg_latency_values, color='orange', alpha=0.8)
        ax3.set_xlabel('Scheduler')
        ax3.set_ylabel('Average Query Time (ms)')
        ax3.set_title('Search Latency (Lower is Better)')
        ax3.tick_params(axis='x', rotation=45)
        ax3.grid(True, alpha=0.3)
        
        # Add value labels on bars
        for bar in bars3:
            height = bar.get_height()
            ax3.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.2f}',
                    ha='center', va='bottom', fontsize=8)
        
        # Plot 4: Combined Performance Score
        if len(schedulers) > 1:
            # Normalize metrics (higher is better for QPS and recall, lower is better for latency)
            norm_qps = np.array(qps_values) / max(qps_values) if max(qps_values) > 0 else np.zeros(len(qps_values))
            norm_recall = np.array(recall_values)  # Already normalized 0-1
            norm_latency = (1.0 / np.array(avg_latency_values)) if min(avg_latency_values) > 0 else np.zeros(len(avg_latency_values))
            norm_latency = norm_latency / max(norm_latency) if max(norm_latency) > 0 else norm_latency
            
            # Combined score (weighted: QPS 40%, Recall 40%, Latency 20%)
            combined_score = (norm_qps * 0.4 + norm_recall * 0.4 + norm_latency * 0.2)
            
            bars4 = ax4.bar(schedulers, combined_score, color='purple', alpha=0.8)
            ax4.set_xlabel('Scheduler')
            ax4.set_ylabel('Combined Performance Score')
            ax4.set_title('Overall Performance Score\n(Higher is Better)')
            ax4.tick_params(axis='x', rotation=45)
            ax4.grid(True, alpha=0.3)
            
            # Add value labels on bars
            for bar in bars4:
                height = bar.get_height()
                ax4.text(bar.get_x() + bar.get_width()/2., height,
                        f'{height:.2f}',
                        ha='center', va='bottom', fontsize=8)
        
        plt.tight_layout()
        
        # Save figure
        figure_path = os.path.join(self.results_dir, "pyvsag_scheduler_performance.png")
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        print(f"Performance figure saved to {figure_path}")

        pdf_path = os.path.join(self.results_dir, "pyvsag_scheduler_performance.pdf")
        plt.savefig(pdf_path, format='pdf', bbox_inches='tight')
        print(f"Performance PDF saved to {pdf_path}")
        
        # Print summary
        self.print_performance_summary(results)
    
    def print_performance_summary(self, results: dict):
        """Print performance summary"""
        print("\n" + "="*60)
        print("PYVSAG ANN SEARCH SCHEDULER PERFORMANCE SUMMARY")
        print("="*60)
        
        for scheduler_name, result in results.items():
            if "error" in result:
                print(f"\n{scheduler_name:15} ERROR: {result['error']}")
                continue
            
            print(f"\n{scheduler_name:15}")
            print(f"  QPS:             {result.get('qps', 0):8.1f} queries/sec")
            print(f"  Recall:          {result.get('recall', 0):8.3f}")
            print(f"  Avg Latency:     {result.get('avg_query_time_ms', 0):8.2f} ms")
            print(f"  P95 Latency:     {result.get('p95_query_time_ms', 0):8.2f} ms")
            print(f"  P99 Latency:     {result.get('p99_query_time_ms', 0):8.2f} ms")
            print(f"  Build Time:      {result.get('build_time', 0):8.2f} seconds")


def main():
    """Main function for PyVSAG scheduler testing"""
    parser = argparse.ArgumentParser(description="Test schedulers with PyVSAG ANN search")
    parser.add_argument("--results-dir", default="results", 
                       help="Directory to store results")
    parser.add_argument("--production-only", action="store_true", 
                       help="Test only production schedulers")
    parser.add_argument("--dim", type=int, default=128, 
                       help="Vector dimension")
    parser.add_argument("--num-elements", type=int, default=50000, 
                       help="Number of vectors in index")
    parser.add_argument("--num-queries", type=int, default=1000, 
                       help="Number of search queries")
    parser.add_argument("--k", type=int, default=10, 
                       help="Number of nearest neighbors to find")
    parser.add_argument("--max-degree", type=int, default=16, 
                       help="HNSW max degree")
    parser.add_argument("--ef-construction", type=int, default=200, 
                       help="HNSW ef construction parameter")
    parser.add_argument("--ef-search", type=int, default=100, 
                       help="HNSW ef search parameter")
    parser.add_argument("--timeout", type=int, default=300, 
                       help="Timeout in seconds")
    parser.add_argument("--scheduler", type=str, default=None,
                       help="Test specific scheduler only")
    parser.add_argument("--schedulers", nargs="+", default=None,
                       help="Explicit scheduler list to test after default")
    
    args = parser.parse_args()
    
    # Check if PyVSAG is available
    if not PYVSAG_AVAILABLE:
        print(f"Error: PyVSAG is not available: {IMPORT_ERROR}")
        print("Please install PyVSAG using: pip install pyvsag")
        sys.exit(1)
    
    # Create tester instance
    tester = PyVSAGBenchmarkTester(args.results_dir)
    
    # Update test parameters
    tester.set_test_params(
        dim=args.dim,
        num_elements=args.num_elements,
        num_queries=args.num_queries,
        k=args.k,
        max_degree=args.max_degree,
        ef_construction=args.ef_construction,
        ef_search=args.ef_search,
        timeout=args.timeout
    )
    
    if args.scheduler:
        print(f"Testing scheduler: {args.scheduler}")
        result = tester.run_pyvsag_benchmark(args.scheduler)
        results = {args.scheduler: result}
    else:
        print("Starting PyVSAG ANN search scheduler performance tests...")
        results = tester.run_all_pyvsag_benchmarks(
            production_only=args.production_only,
            scheduler_names=args.schedulers,
        )
    
    tester.save_results(results)

    # Generate figures
    tester.generate_performance_figures(results)
    
    print("\nTesting complete!")


if __name__ == "__main__":
    main()
