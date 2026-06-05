#!/usr/bin/env python3
"""
Analyze Memtier benchmark results from CSV and create comparison plots
Each scheduler gets a subplot comparing it with default scheduler
"""

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
import os

def load_results(csv_file='memtier_scheduler_results.csv'):
    """Load results from CSV file"""
    if not os.path.exists(csv_file):
        print(f"Error: {csv_file} not found")
        sys.exit(1)
    
    df = pd.read_csv(csv_file)
    
    # Fix total metrics if they are zero but GET/SET metrics exist
    df['total_ops_per_sec'] = df.apply(
        lambda row: row['gets_ops_per_sec'] + row['sets_ops_per_sec'] 
        if row['total_ops_per_sec'] == 0 else row['total_ops_per_sec'], axis=1)
    
    # Use average of GET/SET latencies if total is zero
    df['total_p50_latency_ms'] = df.apply(
        lambda row: (row['gets_p50_latency_ms'] + row['sets_p50_latency_ms']) / 2 
        if row['total_p50_latency_ms'] == 0 and (row['gets_p50_latency_ms'] > 0 or row['sets_p50_latency_ms'] > 0)
        else row['total_p50_latency_ms'], axis=1)
    
    df['total_p99_latency_ms'] = df.apply(
        lambda row: (row['gets_p99_latency_ms'] + row['sets_p99_latency_ms']) / 2 
        if row['total_p99_latency_ms'] == 0 and (row['gets_p99_latency_ms'] > 0 or row['sets_p99_latency_ms'] > 0)
        else row['total_p99_latency_ms'], axis=1)
    
    print(f"Loaded {len(df)} rows from {csv_file}")
    print(f"Schedulers found: {df['scheduler'].unique()}")
    return df

def create_detailed_comparison(df):
    """Create detailed GET/SET comparison for each scheduler"""
    
    # Get unique schedulers (excluding default)
    schedulers = [s for s in df['scheduler'].unique() if s != 'default']
    
    if len(schedulers) == 0:
        print("No schedulers to compare (only default found)")
        return
    
    # Get default data
    default_data = df[df['scheduler'] == 'default']
    
    # Test cases
    test_cases = ['mixed_1_10', 'mixed_10_1', 'pipeline_16', 
                  'sequential_pattern', 'gaussian_pattern', 'advanced_gaussian_random']
    
    # Create figure for GET/SET detailed comparison
    n_schedulers = len(schedulers)
    fig = plt.figure(figsize=(20, 8 * n_schedulers))
    
    for idx, scheduler in enumerate(schedulers):
        scheduler_data = df[df['scheduler'] == scheduler]
        
        # Create 4 subplots: GET throughput, GET latency, SET throughput, SET latency
        ax1 = plt.subplot(n_schedulers, 4, idx * 4 + 1)
        ax2 = plt.subplot(n_schedulers, 4, idx * 4 + 2)
        ax3 = plt.subplot(n_schedulers, 4, idx * 4 + 3)
        ax4 = plt.subplot(n_schedulers, 4, idx * 4 + 4)
        
        x = np.arange(len(test_cases))
        width = 0.35
        
        # Extract GET/SET metrics
        default_gets_ops = []
        scheduler_gets_ops = []
        default_gets_p99 = []
        scheduler_gets_p99 = []
        default_sets_ops = []
        scheduler_sets_ops = []
        default_sets_p99 = []
        scheduler_sets_p99 = []
        
        for test in test_cases:
            # Default metrics
            default_test = default_data[default_data['test_case'] == test]
            if not default_test.empty:
                default_gets_ops.append(default_test['gets_ops_per_sec'].values[0])
                default_gets_p99.append(default_test['gets_p99_latency_ms'].values[0])
                default_sets_ops.append(default_test['sets_ops_per_sec'].values[0])
                default_sets_p99.append(default_test['sets_p99_latency_ms'].values[0])
            else:
                default_gets_ops.append(0)
                default_gets_p99.append(0)
                default_sets_ops.append(0)
                default_sets_p99.append(0)
            
            # Scheduler metrics
            sched_test = scheduler_data[scheduler_data['test_case'] == test]
            if not sched_test.empty:
                scheduler_gets_ops.append(sched_test['gets_ops_per_sec'].values[0])
                scheduler_gets_p99.append(sched_test['gets_p99_latency_ms'].values[0])
                scheduler_sets_ops.append(sched_test['sets_ops_per_sec'].values[0])
                scheduler_sets_p99.append(sched_test['sets_p99_latency_ms'].values[0])
            else:
                scheduler_gets_ops.append(0)
                scheduler_gets_p99.append(0)
                scheduler_sets_ops.append(0)
                scheduler_sets_p99.append(0)
        
        # Plot GET throughput
        ax1.bar(x - width/2, default_gets_ops, width, label='Default', color='lightblue', alpha=0.8)
        ax1.bar(x + width/2, scheduler_gets_ops, width, label=scheduler, color='darkblue', alpha=0.8)
        ax1.set_xlabel('Test Cases')
        ax1.set_ylabel('GET ops/sec')
        ax1.set_title(f'{scheduler}: GET Throughput')
        ax1.set_xticks(x)
        ax1.set_xticklabels([f'T{i+1}' for i in range(len(test_cases))])
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        
        # Plot GET P99 latency
        ax2.bar(x - width/2, default_gets_p99, width, label='Default', color='lightgreen', alpha=0.8)
        ax2.bar(x + width/2, scheduler_gets_p99, width, label=scheduler, color='darkgreen', alpha=0.8)
        ax2.set_xlabel('Test Cases')
        ax2.set_ylabel('GET P99 Latency (ms)')
        ax2.set_title(f'{scheduler}: GET P99 Latency')
        ax2.set_xticks(x)
        ax2.set_xticklabels([f'T{i+1}' for i in range(len(test_cases))])
        ax2.legend()
        ax2.grid(True, alpha=0.3)
        
        # Plot SET throughput
        ax3.bar(x - width/2, default_sets_ops, width, label='Default', color='lightsalmon', alpha=0.8)
        ax3.bar(x + width/2, scheduler_sets_ops, width, label=scheduler, color='darkred', alpha=0.8)
        ax3.set_xlabel('Test Cases')
        ax3.set_ylabel('SET ops/sec')
        ax3.set_title(f'{scheduler}: SET Throughput')
        ax3.set_xticks(x)
        ax3.set_xticklabels([f'T{i+1}' for i in range(len(test_cases))])
        ax3.legend()
        ax3.grid(True, alpha=0.3)
        
        # Plot SET P99 latency
        ax4.bar(x - width/2, default_sets_p99, width, label='Default', color='plum', alpha=0.8)
        ax4.bar(x + width/2, scheduler_sets_p99, width, label=scheduler, color='purple', alpha=0.8)
        ax4.set_xlabel('Test Cases')
        ax4.set_ylabel('SET P99 Latency (ms)')
        ax4.set_title(f'{scheduler}: SET P99 Latency')
        ax4.set_xticks(x)
        ax4.set_xticklabels([f'T{i+1}' for i in range(len(test_cases))])
        ax4.legend()
        ax4.grid(True, alpha=0.3)
    
    plt.suptitle('Detailed GET/SET Performance: Schedulers vs Default', fontsize=16, y=1.02)
    plt.tight_layout()
    
    # Save figure
    output_file = 'memtier_detailed_comparison.png'
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"Detailed comparison plot saved to {output_file}")
    plt.show()

def create_scx_nest_comparison(df):
    """Create Nest-family vs default comparison with throughput and latency subplots"""

    scheduler_name = 'scx_nest_slug' if 'scx_nest_slug' in df['scheduler'].unique() else 'scx_nest'
    scheduler_label = 'DuplexOS-SLUG' if scheduler_name == 'scx_nest_slug' else 'DuplexOS'

    if scheduler_name not in df['scheduler'].unique() or 'default' not in df['scheduler'].unique():
        print(f"Skipping {scheduler_name} comparison - scheduler or default not found in data")
        return
    
    # Get data for selected scheduler and default
    scx_nest_data = df[df['scheduler'] == scheduler_name]
    default_data = df[df['scheduler'] == 'default']
    
    # Test cases
    test_cases = ['mixed_1_10', 'mixed_10_1', 'pipeline_16', 
                  'sequential_pattern', 'advanced_gaussian_random']
    
    # Better names for display
    test_labels = ['Read Heavy', 'Write Heavy', 'Balance Pipeline', 'Balance Sequential', 'Balance Random']
    
    # Create figure with 2 subplots (throughput and latency)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 5))
    
    x = np.arange(len(test_cases))
    width = 0.35
    
    # Extract metrics
    default_throughput = []
    scx_nest_throughput = []
    default_p99 = []
    scx_nest_p99 = []
    
    for test in test_cases:
        # Default metrics
        default_test = default_data[default_data['test_case'] == test]
        if not default_test.empty:
            default_throughput.append(default_test['total_ops_per_sec'].values[0])
            default_p99.append(default_test['total_p99_latency_ms'].values[0])
        else:
            default_throughput.append(0)
            default_p99.append(0)
        
        # scx_nest metrics
        nest_test = scx_nest_data[scx_nest_data['test_case'] == test]
        if not nest_test.empty:
            scx_nest_throughput.append(nest_test['total_ops_per_sec'].values[0])
            scx_nest_p99.append(nest_test['total_p99_latency_ms'].values[0])
        else:
            scx_nest_throughput.append(0)
            scx_nest_p99.append(0)
    
    # Plot throughput comparison
    bars1 = ax1.bar(x - width/2, default_throughput, width, label='Default', color='#4472C4', alpha=0.8)
    bars2 = ax1.bar(x + width/2, scx_nest_throughput, width, label=scheduler_label, color='#ED7D31', alpha=0.8)
    
    ax1.set_xlabel('Test Cases', fontsize=20)
    ax1.set_ylabel('Throughput (ops/sec)', fontsize=20)
    ax1.set_title('Throughput Comparison', fontsize=20)
    ax1.set_xticks(x)
    ax1.set_xticklabels(test_labels, rotation=15, fontsize=16)
    ax1.tick_params(axis='y', labelsize=16)
    ax1.grid(True, alpha=0.3, linestyle='--')
    
    # Add value labels on bars
    for bar in bars1:
        height = bar.get_height()
        if height > 0:
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height/1000:.0f}k' if height > 1000 else f'{height:.0f}',
                    ha='center', va='bottom', fontsize=14)
    
    for bar in bars2:
        height = bar.get_height()
        if height > 0:
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height/1000:.0f}k' if height > 1000 else f'{height:.0f}',
                    ha='center', va='bottom', fontsize=14)
    
    # Plot P99 latency comparison (no labels since we'll use shared legend)
    bars3 = ax2.bar(x - width/2, default_p99, width, color='#4472C4', alpha=0.8)
    bars4 = ax2.bar(x + width/2, scx_nest_p99, width, color='#ED7D31', alpha=0.8)
    
    ax2.set_xlabel('Test Cases', fontsize=20)
    ax2.set_ylabel('P99 Latency (ms)', fontsize=20)
    ax2.set_title('P99 Latency Comparison', fontsize=20)
    ax2.set_xticks(x)
    ax2.set_xticklabels(test_labels, rotation=15, fontsize=16)
    ax2.tick_params(axis='y', labelsize=16)
    ax2.grid(True, alpha=0.3, linestyle='--')
    
    # Add value labels on bars
    for bar in bars3:
        height = bar.get_height()
        if height > 0:
            ax2.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.2f}',
                    ha='center', va='bottom', fontsize=14)
    
    for bar in bars4:
        height = bar.get_height()
        if height > 0:
            ax2.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.2f}',
                    ha='center', va='bottom', fontsize=14)
    
    # Single legend for both subplots at the top center
    handles, labels = ax1.get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', ncol=2, fontsize=18,
               bbox_to_anchor=(0.5, 0.98), frameon=False)
    
    plt.tight_layout()
    plt.subplots_adjust(top=0.88)  # Make room for legend
    
    # Save as PDF
    output_file = 'redis_comparison.pdf'
    plt.savefig(output_file, format='pdf', dpi=300, bbox_inches='tight')
    print(f"{scheduler_name} vs default comparison saved to {output_file}")
    
    # Print detailed comparison summary
    print("\n" + "="*100)
    print(f"DETAILED COMPARISON: DEFAULT vs {scheduler_label.upper()} ({scheduler_name})")
    print("="*100)
    
    for i, test in enumerate(test_cases):
        print(f"\n{test_labels[i]} ({test}):")
        print("-" * 50)
        
        # Throughput comparison
        default_tput = default_throughput[i]
        nest_tput = scx_nest_throughput[i]
        if default_tput > 0:
            tput_diff = ((nest_tput - default_tput) / default_tput) * 100
            print(f"  Throughput:")
            print(f"    Default:  {default_tput:>10,.0f} ops/sec")
            print(f"    {scheduler_label}: {nest_tput:>10,.0f} ops/sec")
            print(f"    Difference: {tput_diff:+.1f}% {'(better)' if tput_diff > 0 else '(worse)'}")
        
        # P99 Latency comparison
        default_lat = default_p99[i]
        nest_lat = scx_nest_p99[i]
        if default_lat > 0:
            lat_diff = ((nest_lat - default_lat) / default_lat) * 100
            print(f"  P99 Latency:")
            print(f"    Default:  {default_lat:>8.2f} ms")
            print(f"    {scheduler_label}: {nest_lat:>8.2f} ms")
            print(f"    Difference: {lat_diff:+.1f}% {'(worse)' if lat_diff > 0 else '(better)'}")
            
        # Get GET/SET metrics for more detail
        default_test_data = default_data[default_data['test_case'] == test]
        nest_test_data = scx_nest_data[scx_nest_data['test_case'] == test]
        
        if not default_test_data.empty and not nest_test_data.empty:
            # GET operations
            default_gets = default_test_data['gets_ops_per_sec'].values[0]
            nest_gets = nest_test_data['gets_ops_per_sec'].values[0]
            if default_gets > 0:
                gets_diff = ((nest_gets - default_gets) / default_gets) * 100
                print(f"  GET Operations:")
                print(f"    Default:  {default_gets:>10,.0f} ops/sec")
                print(f"    {scheduler_label}: {nest_gets:>10,.0f} ops/sec")
                print(f"    Difference: {gets_diff:+.1f}%")
            
            # SET operations
            default_sets = default_test_data['sets_ops_per_sec'].values[0]
            nest_sets = nest_test_data['sets_ops_per_sec'].values[0]
            if default_sets > 0:
                sets_diff = ((nest_sets - default_sets) / default_sets) * 100
                print(f"  SET Operations:")
                print(f"    Default:  {default_sets:>10,.0f} ops/sec")
                print(f"    {scheduler_label}: {nest_sets:>10,.0f} ops/sec")
                print(f"    Difference: {sets_diff:+.1f}%")
    
    # Overall summary
    print("\n" + "="*100)
    print("OVERALL SUMMARY")
    print("="*100)
    
    # Calculate averages
    avg_default_tput = np.mean([t for t in default_throughput if t > 0])
    avg_nest_tput = np.mean([t for t in scx_nest_throughput if t > 0])
    avg_default_p99 = np.mean([l for l in default_p99 if l > 0])
    avg_nest_p99 = np.mean([l for l in scx_nest_p99 if l > 0])
    
    avg_tput_diff = ((avg_nest_tput - avg_default_tput) / avg_default_tput) * 100
    avg_lat_diff = ((avg_nest_p99 - avg_default_p99) / avg_default_p99) * 100
    
    print(f"\nAverage Throughput:")
    print(f"  Default:  {avg_default_tput:>10,.0f} ops/sec")
    print(f"  {scheduler_label}: {avg_nest_tput:>10,.0f} ops/sec")
    print(f"  Difference: {avg_tput_diff:+.1f}% {'(better)' if avg_tput_diff > 0 else '(worse)'}")
    
    print(f"\nAverage P99 Latency:")
    print(f"  Default:  {avg_default_p99:>8.2f} ms")
    print(f"  {scheduler_label}: {avg_nest_p99:>8.2f} ms")
    print(f"  Difference: {avg_lat_diff:+.1f}% {'(worse)' if avg_lat_diff > 0 else '(better)'}")
    
    # Find best and worst cases for DuplexOS
    improvements = [(test_labels[i], ((scx_nest_throughput[i] - default_throughput[i]) / default_throughput[i]) * 100) 
                    for i in range(len(test_cases)) if default_throughput[i] > 0]
    
    if improvements:
        best_case = max(improvements, key=lambda x: x[1])
        worst_case = min(improvements, key=lambda x: x[1])
        
        print(f"\nBest improvement for {scheduler_label}: {best_case[0]} ({best_case[1]:+.1f}% throughput)")
        print(f"Worst performance for {scheduler_label}: {worst_case[0]} ({worst_case[1]:+.1f}% throughput)")
    
    plt.show()

def main():
    """Main function"""
    print("Memtier Benchmark Results Analysis")
    print("="*50)
    
    # Load results
    df = load_results()
        
    # Create detailed GET/SET plots
    print("\nGenerating detailed GET/SET plots...")
    create_detailed_comparison(df)
    
    # Create Nest-family vs default comparison
    print("\nGenerating Nest-family vs default comparison...")
    create_scx_nest_comparison(df)
    
    print("\nAnalysis complete!")

if __name__ == "__main__":
    main()
