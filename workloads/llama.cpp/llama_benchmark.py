#!/usr/bin/env python3
"""
Llama.cpp benchmark script to test different thread counts and batch sizes.
Generates performance visualization figures.
"""

import subprocess
import json
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os
import time
from pathlib import Path

# Configuration
LLAMA_BENCH_PATH = "/root/yunwei37/ai-os/workloads/llama.cpp/build/bin/llama-bench"
MODEL_PATH = "/root/.cache/llama.cpp/ggml-org_gemma-3-1b-it-GGUF_gemma-3-1b-it-Q4_K_M.gguf"

# Test parameters
THREAD_COUNTS = [1, 2, 8, 64, 128, 172]
BATCH_SIZES = [128, 256, 512, 1024, 2048]
REPETITIONS = 3

def run_benchmark(threads, batch_size):
    """Run a single benchmark with specified parameters."""
    cmd = [
        LLAMA_BENCH_PATH,
        "-m", MODEL_PATH,
        "-t", str(threads),
        "-b", str(batch_size),
        "-r", str(REPETITIONS),
        "-o", "json",
    ]
    
    # Set LD_LIBRARY_PATH to include the cpu_bin directory
    env = os.environ.copy()
    lib_path = "/home/yunwei37/ai-os/workloads/llama.cpp/cpu_bin"
    if 'LD_LIBRARY_PATH' in env:
        env['LD_LIBRARY_PATH'] = f"{lib_path}:{env['LD_LIBRARY_PATH']}"
    else:
        env['LD_LIBRARY_PATH'] = lib_path
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            print(f"Error running benchmark (t={threads}, b={batch_size}): {result.stderr}")
            return None
    except subprocess.TimeoutExpired:
        print(f"Benchmark timeout (t={threads}, b={batch_size})")
        return None
    except Exception as e:
        print(f"Exception running benchmark (t={threads}, b={batch_size}): {e}")
        return None

def extract_metrics(benchmark_result):
    """Extract performance metrics from benchmark result."""
    if not benchmark_result:
        return None

    # llama-bench may return either a raw list or a dict containing "results"
    if isinstance(benchmark_result, dict):
        benchmark_result = benchmark_result.get("results", [])

    # At this point we expect a list of result objects
    if not isinstance(benchmark_result, list) or not benchmark_result:
        return None

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
    }

def main():
    """Main benchmarking function."""
    print("Starting llama.cpp benchmark...")
    print(f"Model: {MODEL_PATH}")
    print(f"Thread counts: {THREAD_COUNTS}")
    print(f"Batch sizes: {BATCH_SIZES}")
    print(f"Repetitions per test: {REPETITIONS}")
    print()
    
    # Check if model file exists
    if not os.path.exists(MODEL_PATH):
        print(f"Error: Model file not found at {MODEL_PATH}")
        return
    
    # Check if benchmark binary exists
    if not os.path.exists(LLAMA_BENCH_PATH):
        print(f"Error: Benchmark binary not found at {LLAMA_BENCH_PATH}")
        return
    
    results = []
    total_tests = len(THREAD_COUNTS) * len(BATCH_SIZES)
    test_count = 0
    
    for threads in THREAD_COUNTS:
        for batch_size in BATCH_SIZES:
            test_count += 1
            print(f"Running test {test_count}/{total_tests}: threads={threads}, batch_size={batch_size}")
            
            benchmark_result = run_benchmark(threads, batch_size)
            if benchmark_result:
                metrics = extract_metrics(benchmark_result)
                if metrics:
                    results.append({
                        'threads': threads,
                        'batch_size': batch_size,
                        'pp_tps': metrics['pp_tps'],
                        'tg_tps': metrics['tg_tps'],
                        'pp_time': metrics['pp_time'],
                        'tg_time': metrics['tg_time']
                    })
                    print(f"  PP: {metrics['pp_tps']:.2f} tps, TG: {metrics['tg_tps']:.2f} tps")
                else:
                    print("  Failed to extract metrics")
            else:
                print("  Benchmark failed")
            
            time.sleep(1)  # Small delay between tests
    
    if not results:
        print("No successful benchmark results obtained")
        return
    
    # Convert to DataFrame
    df = pd.DataFrame(results)
    
    # Save raw results
    df.to_csv('benchmark_results.csv', index=False)
    print(f"\nSaved {len(results)} benchmark results to benchmark_results.csv")
    
    # Create visualizations
    create_visualizations(df)
    
    # Print summary statistics
    print("\nSummary Statistics:")
    print("=" * 50)
    print(f"Best PP performance: {df['pp_tps'].max():.2f} tps (threads={df.loc[df['pp_tps'].idxmax(), 'threads']}, batch={df.loc[df['pp_tps'].idxmax(), 'batch_size']})")
    print(f"Best TG performance: {df['tg_tps'].max():.2f} tps (threads={df.loc[df['tg_tps'].idxmax(), 'threads']}, batch={df.loc[df['tg_tps'].idxmax(), 'batch_size']})")

def create_visualizations(df):
    """Create performance visualization figures."""
    plt.style.use('seaborn-v0_8')
    fig, axes = plt.subplots(2, 2, figsize=(15, 12))
    fig.suptitle('Llama.cpp Performance Benchmark Results', fontsize=16, fontweight='bold')
    
    # 1. Heatmap for PP performance
    pp_pivot = df.pivot(index='threads', columns='batch_size', values='pp_tps')
    sns.heatmap(pp_pivot, annot=True, fmt='.1f', cmap='viridis', ax=axes[0,0])
    axes[0,0].set_title('Prompt Processing Performance (tokens/sec)')
    axes[0,0].set_xlabel('Batch Size')
    axes[0,0].set_ylabel('Thread Count')
    
    # 2. Heatmap for TG performance
    tg_pivot = df.pivot(index='threads', columns='batch_size', values='tg_tps')
    sns.heatmap(tg_pivot, annot=True, fmt='.1f', cmap='plasma', ax=axes[0,1])
    axes[0,1].set_title('Text Generation Performance (tokens/sec)')
    axes[0,1].set_xlabel('Batch Size')
    axes[0,1].set_ylabel('Thread Count')
    
    # 3. Line plot for different thread counts
    for batch_size in BATCH_SIZES:
        subset = df[df['batch_size'] == batch_size]
        axes[1,0].plot(subset['threads'], subset['tg_tps'], marker='o', label=f'Batch {batch_size}')
    axes[1,0].set_xlabel('Thread Count')
    axes[1,0].set_ylabel('Text Generation (tokens/sec)')
    axes[1,0].set_title('TG Performance vs Thread Count')
    axes[1,0].legend()
    axes[1,0].grid(True, alpha=0.3)
    
    # 4. Line plot for different batch sizes
    for threads in THREAD_COUNTS:
        subset = df[df['threads'] == threads]
        axes[1,1].plot(subset['batch_size'], subset['tg_tps'], marker='s', label=f'{threads} threads')
    axes[1,1].set_xlabel('Batch Size')
    axes[1,1].set_ylabel('Text Generation (tokens/sec)')
    axes[1,1].set_title('TG Performance vs Batch Size')
    axes[1,1].legend()
    axes[1,1].grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig('llama_benchmark_results.png', dpi=300, bbox_inches='tight')
    plt.savefig('llama_benchmark_results.pdf', bbox_inches='tight')
    print("Saved performance figures: llama_benchmark_results.png and llama_benchmark_results.pdf")
    
    # Create separate detailed plots
    create_detailed_plots(df)

def create_detailed_plots(df):
    """Create additional detailed visualization plots."""
    
    # Performance comparison bar chart
    fig, ax = plt.subplots(1, 1, figsize=(12, 8))
    
    # Find best configurations
    best_configs = df.nlargest(10, 'tg_tps')
    config_labels = [f"T{row['threads']}_B{row['batch_size']}" for _, row in best_configs.iterrows()]
    
    x_pos = range(len(config_labels))
    ax.bar(x_pos, best_configs['tg_tps'], color='skyblue', alpha=0.7)
    ax.set_xlabel('Configuration (Threads_BatchSize)')
    ax.set_ylabel('Text Generation Performance (tokens/sec)')
    ax.set_title('Top 10 Performing Configurations')
    ax.set_xticks(x_pos)
    ax.set_xticklabels(config_labels, rotation=45)
    
    # Add value labels on bars
    for i, v in enumerate(best_configs['tg_tps']):
        ax.text(i, v + 0.5, f'{v:.1f}', ha='center', va='bottom')
    
    plt.tight_layout()
    plt.savefig('llama_benchmark_top_configs.png', dpi=300, bbox_inches='tight')
    print("Saved top configurations plot: llama_benchmark_top_configs.png")
    
    plt.show()

if __name__ == "__main__":
    main()
