#!/usr/bin/env python3
"""
Scheduler Runner Module - Reusable scheduler management and testing utilities
Provides a unified interface for running and managing different schedulers in the schedcp project.
"""

import os
import sys
import subprocess
import time
import json
from typing import Dict, List, Tuple, Optional
import signal


class SchedulerRunner:
    """
    A reusable class for managing and running different schedulers.
    
    This class provides methods to:
    - Load scheduler configurations
    - Start and stop scheduler processes
    - Run commands with schedulers
    - Parse scheduler outputs
    """
    
    def __init__(self, scheduler_bin_path: str = None, scheduler_config_path: str = None):
        """
        Initialize the SchedulerRunner.
        
        Args:
            scheduler_bin_path: Path to scheduler binaries directory
            scheduler_config_path: Path to scheduler configuration file
        """
        # Set default paths relative to this file's location
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.scheduler_bin_path = scheduler_bin_path or os.path.join(base_dir, "sche_bin")
        self.scheduler_config_path = scheduler_config_path or os.path.join(base_dir, "schedulers.json")
        
        # Load scheduler configurations
        self.schedulers = self._load_scheduler_config()
        
        # Track running processes
        self._running_processes = {}
    
    def _load_scheduler_config(self) -> Dict:
        """Load scheduler configuration from JSON file"""
        try:
            with open(self.scheduler_config_path, 'r') as f:
                config = json.load(f)
                # Extract schedulers from nested structure and convert to expected format
                schedulers = {}
                scheduler_list = config.get("schedulers", [])
                
                # Handle both list and dict formats
                if isinstance(scheduler_list, list):
                    for sched in scheduler_list:
                        name = sched.get("name")
                        if name:
                            schedulers[name] = {
                                "binary": sched.get("binary", name),
                                "production": sched.get("production_ready", False),
                                "description": sched.get("description", ""),
                                "parameters": sched.get("tuning_parameters", {}),
                                "args": sched.get("args", [])
                            }
                elif isinstance(scheduler_list, dict):
                    for name, info in scheduler_list.items():
                        schedulers[name] = {
                            "binary": info.get("binary", name),
                            "production": info.get("production_ready", False),
                            "description": info.get("description", ""),
                            "parameters": info.get("parameters", {}),
                            "args": info.get("args", [])
                        }
                return schedulers
        except FileNotFoundError:
            print(f"Warning: Could not find scheduler config at {self.scheduler_config_path}")
            return self._get_default_schedulers()
        except json.JSONDecodeError as e:
            print(f"Warning: Invalid JSON in scheduler config: {e}")
            return self._get_default_schedulers()
    
    def _get_default_schedulers(self) -> Dict:
        """Fallback scheduler list if config file is not available"""
        return {
            "scx_simple": {"binary": "scx_simple", "production": True, "description": "Simple scheduler"},
            "scx_rusty": {"binary": "scx_rusty", "production": True, "description": "Rusty scheduler"},
            "scx_bpfland": {"binary": "scx_bpfland", "production": True, "description": "BPF-based scheduler"},
            "scx_flash": {"binary": "scx_flash", "production": True, "description": "Flash scheduler"},
            "scx_lavd": {"binary": "scx_lavd", "production": True, "description": "LAVD scheduler"},
            "scx_layered": {"binary": "scx_layered", "production": True, "description": "Layered scheduler"},
            "scx_nest": {"binary": "scx_nest", "production": True, "description": "Nest scheduler"},
            "scx_p2dq": {"binary": "scx_p2dq", "production": True, "description": "P2DQ scheduler"},
            "scx_flatcg": {"binary": "scx_flatcg", "production": True, "description": "Flat CGroup scheduler"},
        }
    
    def get_available_schedulers(self, production_only: bool = False) -> List[str]:
        """
        Get list of available scheduler names.
        
        Args:
            production_only: Only return production-ready schedulers
            
        Returns:
            List of scheduler names
        """
        schedulers = []
        for name, info in self.schedulers.items():
            if production_only and not info.get("production", False):
                continue
            schedulers.append(name)
        return schedulers
    
    def get_scheduler_info(self, scheduler_name: str) -> Dict:
        """
        Get information about a specific scheduler.
        
        Args:
            scheduler_name: Name of the scheduler
            
        Returns:
            Dictionary containing scheduler information
        """
        return self.schedulers.get(scheduler_name, {})
    
    def start_scheduler(self, scheduler_name: str, args: List[str] = None, 
                       wait_time: int = 2) -> subprocess.Popen:
        """
        Start a scheduler process.
        
        Args:
            scheduler_name: Name of the scheduler to start
            args: Additional command line arguments
            wait_time: Time to wait for scheduler to initialize
            
        Returns:
            Process object for the started scheduler
            
        Raises:
            ValueError: If scheduler is unknown
            FileNotFoundError: If scheduler binary is not found
            RuntimeError: If scheduler fails to start
        """
        scheduler_info = self.schedulers.get(scheduler_name)
        if not scheduler_info:
            raise ValueError(f"Unknown scheduler: {scheduler_name}")
        
        binary_path = os.path.join(self.scheduler_bin_path, scheduler_info["binary"])
        
        # Check if binary exists
        if not os.path.exists(binary_path):
            raise FileNotFoundError(f"Scheduler binary not found: {binary_path}")
        
        # Build command
        scheduler_args = list(scheduler_info.get("args", []))

        if args:
            scheduler_args.extend(args)

        self._ensure_bpf_pin_dirs(scheduler_args)

        cmd = ["sudo", binary_path]
        
        # Special handling for scx_layered - add --run-example if no config specified
        if scheduler_name == "scx_layered" and not any("--config" in arg or "-c" in arg for arg in scheduler_args):
            cmd.append("--run-example")
        
        if scheduler_args:
            cmd.extend(scheduler_args)
        
        # Start scheduler process
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid  # Create new process group
            )
            
            # Give scheduler time to initialize
            time.sleep(wait_time)
            
            # Check if process is still running
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                raise RuntimeError(f"Scheduler failed to start: {stderr}")
            
            # Track the process
            self._running_processes[scheduler_name] = proc
            
            return proc
            
        except Exception as e:
            raise RuntimeError(f"Failed to start scheduler {scheduler_name}: {e}")

    def _ensure_bpf_pin_dirs(self, args: List[str]):
        """Create parent directories for scheduler BPF pin paths when configured."""
        for idx, arg in enumerate(args):
            if arg not in ("-H", "--slug-hint-map") or idx + 1 >= len(args):
                continue

            parent = os.path.dirname(args[idx + 1])
            if not parent:
                continue

            subprocess.run(
                ["sudo", "mkdir", "-p", parent],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
    
    def stop_scheduler(self, scheduler_name: str = None, proc: subprocess.Popen = None):
        """
        Stop a scheduler process.
        
        Args:
            scheduler_name: Name of the scheduler to stop
            proc: Process object to stop (alternative to scheduler_name)
        """
        if scheduler_name and scheduler_name in self._running_processes:
            proc = self._running_processes.pop(scheduler_name)
        
        if proc and proc.poll() is None:
            try:
                # Try to terminate gracefully first
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if graceful termination fails
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait()
            except (OSError, ProcessLookupError):
                # Process already terminated
                pass
    
    def stop_all_schedulers(self):
        """Stop all running scheduler processes"""
        for scheduler_name in list(self._running_processes.keys()):
            self.stop_scheduler(scheduler_name)
    
    def run_command_with_scheduler(self, scheduler_name: str, command: List[str], 
                                  scheduler_args: List[str] = None,
                                  timeout: int = 60, env: dict = None) -> Tuple[int, str, str]:
        """
        Run a command with a specific scheduler active.
        
        Args:
            scheduler_name: Name of the scheduler to use
            command: Command to run
            scheduler_args: Additional arguments for the scheduler
            timeout: Command timeout in seconds
            env: Environment variables to pass to the command
            
        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        scheduler_proc = None
        try:
            # Start scheduler
            scheduler_proc = self.start_scheduler(scheduler_name, scheduler_args)
            
            # Run command
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
            
            return result.returncode, result.stdout, result.stderr
            
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except Exception as e:
            return -1, "", str(e)
        finally:
            # Stop scheduler
            if scheduler_proc:
                self.stop_scheduler(proc=scheduler_proc)
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure all processes are stopped"""
        self.stop_all_schedulers()


class SchedulerBenchmark:
    """
    A class for benchmarking schedulers with various tools.
    
    This class extends SchedulerRunner to provide benchmarking capabilities.
    """
    
    def __init__(self, scheduler_runner: SchedulerRunner = None):
        """
        Initialize the SchedulerBenchmark.
        
        Args:
            scheduler_runner: SchedulerRunner instance to use
        """
        self.runner = scheduler_runner or SchedulerRunner()
    
    def benchmark_scheduler(self, scheduler_name: str, benchmark_cmd: List[str],
                           scheduler_args: List[str] = None,
                           timeout: int = 60) -> Dict:
        """
        Run a benchmark with a specific scheduler.
        
        Args:
            scheduler_name: Name of the scheduler to benchmark
            benchmark_cmd: Benchmark command to run
            scheduler_args: Additional arguments for the scheduler
            timeout: Benchmark timeout in seconds
            
        Returns:
            Dictionary containing benchmark results
        """
        print(f"Running benchmark with scheduler: {scheduler_name}")
        
        exit_code, stdout, stderr = self.runner.run_command_with_scheduler(
            scheduler_name, benchmark_cmd, scheduler_args, timeout
        )
        
        return {
            "scheduler": scheduler_name,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "success": exit_code == 0
        }
    
    def benchmark_all_schedulers(self, benchmark_cmd: List[str],
                                scheduler_args: Dict[str, List[str]] = None,
                                production_only: bool = True,
                                timeout: int = 60) -> Dict[str, Dict]:
        """
        Run benchmark with all available schedulers.
        
        Args:
            benchmark_cmd: Benchmark command to run
            scheduler_args: Dictionary of scheduler-specific arguments
            production_only: Only test production-ready schedulers
            timeout: Benchmark timeout in seconds
            
        Returns:
            Dictionary mapping scheduler names to benchmark results
        """
        results = {}
        scheduler_args = scheduler_args or {}
        
        # Get list of schedulers to test
        schedulers = self.runner.get_available_schedulers(production_only)
        
        for scheduler_name in schedulers:
            try:
                args = scheduler_args.get(scheduler_name, [])
                result = self.benchmark_scheduler(
                    scheduler_name, benchmark_cmd, args, timeout
                )
                results[scheduler_name] = result
                
                # Brief pause between tests
                time.sleep(1)
                
            except Exception as e:
                print(f"Error benchmarking scheduler {scheduler_name}: {e}")
                results[scheduler_name] = {
                    "scheduler": scheduler_name,
                    "error": str(e),
                    "success": False
                }
        
        return results


def main():
    """Example usage of the SchedulerRunner"""
    # Create a scheduler runner
    runner = SchedulerRunner()
    
    # List available schedulers
    print("Available schedulers:")
    for scheduler in runner.get_available_schedulers():
        info = runner.get_scheduler_info(scheduler)
        print(f"  {scheduler}: {info.get('description', 'No description')}")
    
    # Example: Run a command with a specific scheduler
    print("\nRunning 'sleep 5' with scx_simple scheduler...")
    exit_code, stdout, stderr = runner.run_command_with_scheduler(
        "scx_simple", ["sleep", "5"]
    )
    print(f"Command completed with exit code: {exit_code}")


if __name__ == "__main__":
    main()
