#!/usr/bin/env python3
"""
Utility functions for Redis benchmarking scripts.
This module contains shared functions used by both redis_benchmark.py and redis_bench_start.py
"""

import os
import subprocess
import glob
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Any


class RedisCleanup:
    """Utility class for cleaning up Redis-related files"""
    
    @staticmethod
    def cleanup_redis_files(verbose: bool = True):
        """
        Clean up Redis temporary files including .rdb and config files
        
        Args:
            verbose: Whether to print cleanup messages
        """
        if verbose:
            print("[INFO] Cleaning up Redis temporary files")
        
        # Clean up redis config files
        try:
            config_files = glob.glob("redis_config_*.conf")
            for conf_file in config_files:
                try:
                    os.remove(conf_file)
                    if verbose:
                        print(f"[INFO] Removed config file: {conf_file}")
                except Exception as e:
                    if verbose:
                        print(f"[WARNING] Failed to remove config file {conf_file}: {e}")
        except Exception as e:
            if verbose:
                print(f"[WARNING] Failed to clean up config files: {e}")
        
        # Clean up Redis dump files
        try:
            # Common Redis dump file names
            standard_rdb_files = ["dump.rdb", "temp.rdb", "appendonly.aof"]
            for rdb_file in standard_rdb_files:
                if os.path.exists(rdb_file):
                    try:
                        os.remove(rdb_file)
                        if verbose:
                            print(f"[INFO] Removed Redis file: {rdb_file}")
                    except Exception as e:
                        if verbose:
                            print(f"[WARNING] Failed to remove {rdb_file}: {e}")
            
            # Clean up any other .rdb files
            other_rdb_files = glob.glob("*.rdb")
            for rdb_file in other_rdb_files:
                if rdb_file not in standard_rdb_files:
                    try:
                        os.remove(rdb_file)
                        if verbose:
                            print(f"[INFO] Removed Redis dump file: {rdb_file}")
                    except Exception as e:
                        if verbose:
                            print(f"[WARNING] Failed to remove dump file {rdb_file}: {e}")
        except Exception as e:
            if verbose:
                print(f"[WARNING] Failed to clean up .rdb files: {e}")
    
    @staticmethod
    def kill_redis_processes(timeout: int = 5):
        """
        Kill any running Redis server processes
        
        Args:
            timeout: Timeout in seconds for the kill command
        """
        try:
            result = subprocess.run(
                ["pkill", "-f", "redis-server"], 
                capture_output=True, 
                timeout=timeout
            )
            if result.returncode == 0:
                print("[INFO] Killed remaining redis-server processes")
            else:
                print("[INFO] No remaining redis-server processes found")
        except subprocess.TimeoutExpired:
            print("[WARNING] pkill redis-server timed out")
        except Exception as e:
            print(f"[WARNING] Failed to kill Redis processes: {e}")


class RedisConfig:
    """Utility class for Redis configuration management"""
    
    @staticmethod
    def generate_config_file(config_options: Dict[str, Any] = None) -> str:
        """
        Generate a Redis configuration file with specified options
        
        Args:
            config_options: Dictionary of Redis configuration options
            
        Returns:
            Path to the generated configuration file
        """
        config_options = config_options or {}
        
        config_lines = [
            "# Dynamically generated Redis configuration",
            "port 6379",
            "bind 127.0.0.1",
            "daemonize no",
            "loglevel warning",
            "maxclients 10000",
            "timeout 0",
            "tcp-keepalive 300",
            "tcp-backlog 511",
            ""
        ]
        
        # Add custom configuration options
        for key, value in config_options.items():
            if key == "io_threads" and value > 1:
                # Validate io_threads value
                if value > 128:
                    print(f"[WARNING] io_threads value {value} is very high, capping at 128")
                    value = 128
                elif value < 1:
                    print(f"[WARNING] io_threads value {value} is invalid, setting to 1")
                    value = 1
                config_lines.append(f"io-threads {value}")
            elif key == "io_threads_do_reads":
                config_lines.append(f"io-threads-do-reads {value}")
            elif key == "threads":
                config_lines.append(f"threads {value}")
            elif key == "maxmemory":
                config_lines.append(f"maxmemory {value}")
            elif key == "maxmemory_policy":
                config_lines.append(f"maxmemory-policy {value}")
            elif key == "hz":
                if value > 500:
                    print(f"[WARNING] hz value {value} is very high, may cause issues")
                config_lines.append(f"hz {value}")
            elif key == "client_output_buffer_limit":
                config_lines.append(f"client-output-buffer-limit normal {value}")
            else:
                # Generic key-value pairs
                config_lines.append(f"{key.replace('_', '-')} {value}")
        
        # Write to temporary file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config_file = f"redis_config_{timestamp}.conf"
        
        try:
            with open(config_file, 'w') as f:
                f.write('\n'.join(config_lines))
            
            print(f"[INFO] Redis configuration written to: {config_file}")
            print(f"[INFO] Configuration contains {len(config_lines)} lines")
            
            if config_options:
                print(f"[INFO] Custom config options applied: {list(config_options.keys())}")
            
            return config_file
        except Exception as e:
            print(f"[ERROR] Failed to write Redis configuration file: {e}")
            raise


class BenchmarkParser:
    """Utility class for parsing benchmark results"""
    
    @staticmethod
    def parse_csv_output(csv_output: str) -> Optional[List[Dict]]:
        """
        Parse CSV output from redis-benchmark into structured data
        
        Args:
            csv_output: Raw CSV output string
            
        Returns:
            List of dictionaries containing parsed metrics
        """
        lines = csv_output.strip().split('\n')
        if len(lines) < 2:
            return None
        
        # Parse header
        header_line = lines[0].strip('"')
        headers = [h.strip('"') for h in header_line.split('","')]
        
        results = []
        for line in lines[1:]:
            if line.strip():
                # Parse data line
                data_line = line.strip('"')
                values = [v.strip('"') for v in data_line.split('","')]
                
                if len(values) == len(headers):
                    row = {}
                    for i, header in enumerate(headers):
                        value = values[i]
                        # Try to convert numeric values
                        try:
                            if '.' in value:
                                row[header] = float(value)
                            else:
                                row[header] = int(value)
                        except ValueError:
                            row[header] = value
                    results.append(row)
        
        return results
    
    @staticmethod
    def parse_benchmark_output(output: str) -> Dict[str, Any]:
        """
        Parse Redis benchmark output for metrics
        
        Args:
            output: Raw benchmark output string
            
        Returns:
            Dictionary containing parsed metrics
        """
        metrics = {}
        lines = output.split('\n')
        
        current_test = None
        for line in lines:
            # Detect test type (e.g., "====== SET ======")
            if line.startswith("======") and "======" in line[6:]:
                current_test = line.replace("=", "").strip()
                continue
            
            # Parse CSV output if present
            if '"' in line and ',' in line:
                try:
                    parts = line.strip().strip('"').split('","')
                    if len(parts) >= 2:
                        test_name = parts[0]
                        rps = float(parts[1])
                        if test_name not in metrics:
                            metrics[test_name] = {}
                        metrics[test_name]['requests_per_second'] = rps
                except:
                    pass
            
            # Parse regular output
            elif 'requests per second' in line.lower():
                # Extract RPS
                parts = line.split()
                for i, part in enumerate(parts):
                    cleaned = part.replace(',', '').replace('.', '', part.count('.') - 1)
                    try:
                        rps = float(cleaned)
                        if current_test:
                            if current_test not in metrics:
                                metrics[current_test] = {}
                            metrics[current_test]['requests_per_second'] = rps
                        else:
                            metrics['requests_per_second'] = rps
                        break
                    except:
                        continue
            
            elif 'latency summary' in line.lower():
                current_test = "latency"
            
            # Parse percentile latencies
            elif '%' in line and ('percentile' in line.lower() or 'latency' in line.lower()):
                percentiles = ['50.00', '95.00', '99.00', '99.90']
                for p in percentiles:
                    if p + '%' in line:
                        latency = BenchmarkParser.extract_latency(line)
                        if latency > 0:
                            key = f'latency_p{p.split(".")[0]}'
                            if current_test and current_test != "latency":
                                if current_test not in metrics:
                                    metrics[current_test] = {}
                                metrics[current_test][key] = latency
                            else:
                                metrics[key] = latency
        
        return metrics
    
    @staticmethod
    def extract_latency(line: str) -> float:
        """
        Extract latency value from a line
        
        Args:
            line: Line containing latency information
            
        Returns:
            Extracted latency value or 0.0 if not found
        """
        parts = line.split()
        for part in parts:
            if part.replace('.', '').isdigit():
                return float(part)
        return 0.0


class ProcessManager:
    """Utility class for managing Redis and scheduler processes"""
    
    @staticmethod
    def stop_redis_gracefully(redis_cli_path: str, timeout: int = 2) -> bool:
        """
        Attempt to stop Redis server gracefully
        
        Args:
            redis_cli_path: Path to redis-cli binary
            timeout: Timeout in seconds
            
        Returns:
            True if successful, False otherwise
        """
        try:
            result = subprocess.run(
                [redis_cli_path, "shutdown", "nosave"],
                capture_output=True,
                timeout=timeout
            )
            if result.returncode == 0:
                print("[INFO] Redis graceful shutdown successful")
                return True
            else:
                print(f"[WARNING] Redis graceful shutdown returned code {result.returncode}")
                return False
        except subprocess.TimeoutExpired:
            print("[WARNING] Redis graceful shutdown timed out")
            return False
        except Exception as e:
            print(f"[WARNING] Redis graceful shutdown failed: {e}")
            return False
    
    @staticmethod
    def check_redis_running(redis_cli_path: str) -> bool:
        """
        Check if Redis server is running
        
        Args:
            redis_cli_path: Path to redis-cli binary
            
        Returns:
            True if Redis is running, False otherwise
        """
        try:
            result = subprocess.run(
                [redis_cli_path, "ping"],
                check=True,
                capture_output=True,
                timeout=2,
                text=True
            )
            return "PONG" in result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
    
    @staticmethod
    def kill_scheduler_processes(timeout: int = 5):
        """
        Kill any running scheduler processes
        
        Args:
            timeout: Timeout in seconds
        """
        try:
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            scheduler_glob = os.path.join(repo_root, "scheduler", "sche_bin", "scx_*")
            scheduler_names = sorted({os.path.basename(path)[:15]
                                      for path in glob.glob(scheduler_glob)
                                      if os.path.isfile(path)})
            killed = False

            for name in scheduler_names:
                result = subprocess.run(
                    ["sudo", "pkill", "-x", name],
                    capture_output=True,
                    timeout=timeout
                )
                killed = killed or result.returncode == 0

            if killed:
                print("[INFO] Successfully killed lingering scx processes")
            else:
                print("[INFO] No lingering scx processes found")
        except Exception as e:
            print(f"[WARNING] Error killing lingering scx processes: {e}")


class BenchmarkSummary:
    """Utility class for generating benchmark summaries"""
    
    @staticmethod
    def generate_summary(results: List[Dict], config_options: Dict = None) -> Dict:
        """
        Generate a summary from benchmark results
        
        Args:
            results: List of benchmark result dictionaries
            config_options: Redis configuration options used
            
        Returns:
            Dictionary containing summary statistics
        """
        summary = {
            "total_tests": len(results) if results else 0,
            "successful_tests": sum(1 for r in results if r.get("return_code") == 0) if results else 0,
            "failed_tests": sum(1 for r in results if r.get("return_code") != 0) if results else 0,
            "total_duration": sum(r.get("duration", 0) for r in results) if results else 0,
            "redis_config": config_options or {},
            "test_summary": []
        }
        
        if results:
            for result in results:
                test_summary = {
                    "test_name": result.get("test_name", "unknown"),
                    "status": "success" if result.get("return_code") == 0 else "failed",
                    "duration": result.get("duration", 0),
                    "metrics": result.get("metrics", {}),
                    "parsed_metrics": result.get("parsed_metrics", [])
                }
                summary["test_summary"].append(test_summary)
        
        return summary
    
    @staticmethod
    def print_summary(summary: Dict):
        """
        Print a formatted summary of benchmark results
        
        Args:
            summary: Summary dictionary from generate_summary
        """
        print("\n" + "=" * 50)
        print("BENCHMARK SUMMARY")
        print("=" * 50)
        print(f"Total tests: {summary['total_tests']}")
        print(f"Successful: {summary['successful_tests']}")
        print(f"Failed: {summary['failed_tests']}")
        print(f"Total duration: {summary['total_duration']:.2f} seconds")
        
        if summary.get('redis_config'):
            print(f"Redis config: {summary['redis_config']}")
        
        print("\nTest Results:")
        for test in summary.get('test_summary', []):
            status_symbol = "✓" if test['status'] == 'success' else "✗"
            print(f"  {status_symbol} {test['test_name']}: {test['duration']:.2f}s")
            
            # Print metrics if available
            metrics = test.get('metrics', {})
            if isinstance(metrics, dict):
                for key, value in metrics.items():
                    if isinstance(value, dict):
                        for k, v in value.items():
                            print(f"      {k}: {v}")
                    elif 'requests_per_second' in key:
                        print(f"    RPS: {value:,.0f}")
                    elif 'latency' in key:
                        print(f"    {key}: {value:.2f}ms")
