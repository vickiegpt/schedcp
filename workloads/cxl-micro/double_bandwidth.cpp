/**
 * double_bandwidth.cpp - CXL double bandwidth microbenchmark
 *
 * This microbenchmark measures the bandwidth of CXL by adjusting
 * the ratio of readers to writers, simulating bidirectional traffic.
 */

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <getopt.h>
#include <iostream>
#include <mutex>
#include <numa.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include "thread_workers.hpp"

// Default parameters
constexpr size_t DEFAULT_BUFFER_SIZE = 1 * 1024 * 1024 * 1024UL; // 1GB
constexpr size_t DEFAULT_BLOCK_SIZE = 4096;                      // 4KB
constexpr int DEFAULT_DURATION = 30;                             // seconds
constexpr int DEFAULT_NUM_THREADS = 500;    // total threads
constexpr float DEFAULT_READ_RATIO = 0.5;   // 50% readers, 50% writers
constexpr size_t DEFAULT_MAX_BANDWIDTH = 0; // 0 means unlimited (MB/s)
constexpr int DEFAULT_NUMA_NODE = 0;        // Default CPU NUMA node

// ThreadStats and RateLimiter are now defined in thread_workers.hpp

struct BenchmarkConfig {
  size_t buffer_size = DEFAULT_BUFFER_SIZE;
  size_t block_size = DEFAULT_BLOCK_SIZE;
  int duration = DEFAULT_DURATION;
  int num_threads = DEFAULT_NUM_THREADS;
  float read_ratio = DEFAULT_READ_RATIO;
  size_t max_bandwidth_mbps = DEFAULT_MAX_BANDWIDTH;
  std::string device_path;
  bool use_mmap = false;
  bool is_cxl_mem = false;
  size_t cpu_workload_size = 0;      // CPU workload size in bytes (default: 0)
  int numa_node = DEFAULT_NUMA_NODE; // NUMA node to bind to
  int memory_numa_node = -1;         // NUMA node for memory allocation (-1 = default)
  bool enable_numa = true;           // Enable NUMA binding
  bool json_output = true;          // Output results in JSON format
  bool random_access = true;         // Random access pattern (default: true)
};

namespace {

[[noreturn]] void die_invalid_arg(const char *option, const char *value,
                                  const char *message) {
  std::cerr << "Invalid value for " << option;
  if (value) {
    std::cerr << ": " << value;
  }
  std::cerr << " (" << message << ")\n";
  std::exit(1);
}

int parse_int_arg(const char *option, const char *value) {
  if (!value) {
    die_invalid_arg(option, value, "missing argument");
  }

  try {
    return std::stoi(value);
  } catch (const std::exception &) {
    die_invalid_arg(option, value, "expected integer");
  }
}

size_t parse_size_arg(const char *option, const char *value) {
  if (!value) {
    die_invalid_arg(option, value, "missing argument");
  }

  try {
    return std::stoull(value);
  } catch (const std::exception &) {
    die_invalid_arg(option, value, "expected unsigned integer");
  }
}

float parse_float_arg(const char *option, const char *value) {
  if (!value) {
    die_invalid_arg(option, value, "missing argument");
  }

  try {
    return std::stof(value);
  } catch (const std::exception &) {
    die_invalid_arg(option, value, "expected floating-point number");
  }
}

bool node_has_allowed_cpus(int node) {
  struct bitmask *node_cpus = numa_allocate_cpumask();
  if (!node_cpus) {
    return false;
  }

  bool has_allowed_cpu = false;
  if (numa_node_to_cpus(node, node_cpus) == 0) {
    const int cpu_count = node_cpus->size;
    cpu_set_t *allowed = CPU_ALLOC(cpu_count);
    if (allowed) {
      const size_t allowed_size = CPU_ALLOC_SIZE(cpu_count);
      CPU_ZERO_S(allowed_size, allowed);

      if (sched_getaffinity(0, allowed_size, allowed) == 0) {
        for (int cpu = 0; cpu < cpu_count; ++cpu) {
          if (numa_bitmask_isbitset(node_cpus, cpu) &&
              CPU_ISSET_S(cpu, allowed_size, allowed)) {
            has_allowed_cpu = true;
            break;
          }
        }
      }

      CPU_FREE(allowed);
    }
  }

  numa_free_cpumask(node_cpus);
  return has_allowed_cpu;
}

} // namespace

void print_usage(const char *prog_name) {
  std::cerr
      << "Usage: " << prog_name << " [OPTIONS]\n"
      << "Options:\n"
      << "  -b, --buffer-size=SIZE    Total buffer size in bytes (default: "
         "1GB)\n"
      << "  -s, --block-size=SIZE     Block size for read/write operations "
         "(default: 4KB)\n"
      << "  -t, --threads=NUM         Total number of threads (default: 4)\n"
      << "  -d, --duration=SECONDS    Test duration in seconds (default: 10)\n"
      << "  -r, --read-ratio=RATIO    Ratio of readers (0.0-1.0, default: "
         "0.5)\n"
      << "  -B, --max-bandwidth=MB/s  Maximum total bandwidth in MB/s "
         "(0=unlimited, default: 0)\n"
      << "  -D, --device=PATH         CXL device path (if not specified, "
         "memory is used)\n"
      << "  -m, --mmap                Use mmap instead of read/write syscalls\n"
      << "  -c, --cxl-mem             Indicate the device is CXL memory\n"
      << "  -w, --cpu-workload=SIZE    CPU workload size in bytes (default: "
         "0)\n"
      << "  -N, --numa-node=NODE      Bind threads to a specific NUMA node "
         "(default: 0)\n"
      << "  -M, --mem-node=NODE       Allocate benchmark memory on a specific "
         "NUMA node\n"
      << "  -n, --no-numa             Disable NUMA binding\n"
      << "  -R, --random              Use random access pattern (default)\n"
      << "  -S, --sequential          Use sequential access pattern\n"
      << "  -j, --json                Output results in JSON format\n"
      << "  -h, --help                Show this help message\n";
}

BenchmarkConfig parse_args(int argc, char *argv[]) {
  BenchmarkConfig config;

  static struct option long_options[] = {
      {"buffer-size", required_argument, 0, 'b'},
      {"block-size", required_argument, 0, 's'},
      {"threads", required_argument, 0, 't'},
      {"duration", required_argument, 0, 'd'},
      {"read-ratio", required_argument, 0, 'r'},
      {"max-bandwidth", required_argument, 0, 'B'},
      {"device", required_argument, 0, 'D'},
      {"mmap", no_argument, 0, 'm'},
      {"cxl-mem", no_argument, 0, 'c'},
      {"cpu-workload", required_argument, 0, 'w'},
      {"numa-node", required_argument, 0, 'N'},
      {"mem-node", required_argument, 0, 'M'},
      {"no-numa", no_argument, 0, 'n'},
      {"random", no_argument, 0, 'R'},
      {"sequential", no_argument, 0, 'S'},
      {"json", no_argument, 0, 'j'},
      {"help", no_argument, 0, 'h'},
      {0, 0, 0, 0}};

  int opt, option_index = 0;
  while ((opt = getopt_long(argc, argv, "b:s:t:d:r:B:D:mchw:N:M:nRSj", long_options,
                            &option_index)) != -1) {
    switch (opt) {
    case 'b':
      config.buffer_size = parse_size_arg("--buffer-size", optarg);
      break;
    case 's':
      config.block_size = parse_size_arg("--block-size", optarg);
      break;
    case 't':
      config.num_threads = parse_int_arg("--threads", optarg);
      break;
    case 'd':
      config.duration = parse_int_arg("--duration", optarg);
      break;
    case 'r':
      config.read_ratio = parse_float_arg("--read-ratio", optarg);
      if (config.read_ratio < 0.0 || config.read_ratio > 1.0) {
        std::cerr << "Read ratio must be between 0.0 and 1.0\n";
        exit(1);
      }
      break;
    case 'B':
      config.max_bandwidth_mbps = parse_size_arg("--max-bandwidth", optarg);
      break;
    case 'D':
      config.device_path = optarg;
      break;
    case 'm':
      config.use_mmap = true;
      break;
    case 'c':
      config.is_cxl_mem = true;
      break;
    case 'w':
      config.cpu_workload_size = parse_size_arg("--cpu-workload", optarg);
      break;
    case 'N':
      config.numa_node = parse_int_arg("--numa-node", optarg);
      break;
    case 'M':
      config.memory_numa_node = parse_int_arg("--mem-node", optarg);
      break;
    case 'n':
      config.enable_numa = false;
      break;
    case 'R':
      config.random_access = true;
      break;
    case 'S':
      config.random_access = false;
      break;
    case 'j':
      config.json_output = true;
      break;
    case 'h':
      print_usage(argv[0]);
      exit(0);
    default:
      print_usage(argv[0]);
      exit(1);
    }
  }

  return config;
}

// Thread functions are now implemented in thread_workers.hpp

int main(int argc, char *argv[]) {
  BenchmarkConfig config = parse_args(argc, argv);
  bool numa_available_on_system = false;

  // Initialize and validate NUMA if enabled
  if (config.enable_numa || config.memory_numa_node >= 0) {
    if (numa_available() == -1) {
      std::cerr
          << "NUMA is not available on this system. Disabling NUMA features."
          << std::endl;
      config.enable_numa = false;
      config.memory_numa_node = -1;
    } else {
      numa_available_on_system = true;
      int max_node = numa_max_node();
      if (config.numa_node > max_node) {
        std::cerr << "Error: NUMA node " << config.numa_node
                  << " does not exist. Maximum node is " << max_node
                  << std::endl;
        return 1;
      }

      if (config.memory_numa_node > max_node) {
        std::cerr << "Error: Memory NUMA node " << config.memory_numa_node
                  << " does not exist. Maximum node is " << max_node
                  << std::endl;
        return 1;
      }

      if (config.enable_numa && !node_has_allowed_cpus(config.numa_node)) {
        std::cerr << "Warning: NUMA node " << config.numa_node
                  << " has no CPUs available to this process. Disabling CPU "
                     "binding.";
        if (config.memory_numa_node < 0) {
          std::cerr << " Use --mem-node=" << config.numa_node
                    << " if you intended to place memory on that node.";
        }
        std::cerr << std::endl;
        config.enable_numa = false;
      }

      if (!config.json_output) {
        std::cout << "NUMA initialized successfully. Available nodes: 0-"
                  << max_node << std::endl;
      }
    }
  }

  // Calculate reader and writer thread counts
  int num_readers = static_cast<int>(config.num_threads * config.read_ratio);
  int num_writers = config.num_threads - num_readers;

  // Create rate limiters for read and write based on ratio
  std::unique_ptr<RateLimiter> read_limiter = nullptr;
  std::unique_ptr<RateLimiter> write_limiter = nullptr;

  if (config.max_bandwidth_mbps > 0) {
    size_t read_bandwidth =
        static_cast<size_t>(config.max_bandwidth_mbps * config.read_ratio);
    size_t write_bandwidth = config.max_bandwidth_mbps - read_bandwidth;

    if (num_readers > 0) {
      read_limiter = std::make_unique<RateLimiter>(read_bandwidth);
    }
    if (num_writers > 0) {
      write_limiter = std::make_unique<RateLimiter>(write_bandwidth);
    }
  }

  if (!config.json_output) {
    std::cout << "=== CXL Double Bandwidth Microbenchmark ===" << std::endl;
    std::cout << "Buffer size: " << config.buffer_size << " bytes" << std::endl;
    std::cout << "Block size: " << config.block_size << " bytes" << std::endl;
    std::cout << "Duration: " << config.duration << " seconds" << std::endl;
    std::cout << "Total threads: " << config.num_threads << std::endl;
    std::cout << "Read ratio: " << config.read_ratio << " (" << num_readers
              << " readers, " << num_writers << " writers)" << std::endl;

    if (config.max_bandwidth_mbps > 0) {
      size_t read_bandwidth =
          static_cast<size_t>(config.max_bandwidth_mbps * config.read_ratio);
      size_t write_bandwidth = config.max_bandwidth_mbps - read_bandwidth;
      std::cout << "Bandwidth limit: " << config.max_bandwidth_mbps
                << " MB/s total "
                << "(" << read_bandwidth << " MB/s read, " << write_bandwidth
                << " MB/s write)" << std::endl;
    } else {
      std::cout << "Bandwidth limit: Unlimited" << std::endl;
    }

    if (!config.device_path.empty()) {
      std::cout << "Device: " << config.device_path << std::endl;
      std::cout << "Access method: " << (config.use_mmap ? "mmap" : "read/write")
                << std::endl;
      std::cout << "Memory type: "
                << (config.is_cxl_mem ? "CXL memory" : "Regular device")
                << std::endl;
    } else {
      std::cout << "Using system memory for testing" << std::endl;
    }

    if (config.enable_numa) {
      std::cout << "NUMA binding: Enabled, binding threads to node " << config.numa_node
                << std::endl;
    } else {
      std::cout << "NUMA binding: Disabled" << std::endl;
    }

    if (config.memory_numa_node >= 0) {
      std::cout << "Memory allocation node: " << config.memory_numa_node
                << std::endl;
    } else {
      std::cout << "Memory allocation node: default policy" << std::endl;
    }

    std::cout << "Access pattern: " << (config.random_access ? "Random" : "Sequential") << std::endl;

    std::cout << "\nStarting benchmark..." << std::endl;
  }

  // Prepare threads and resources
  std::vector<std::thread> threads;
  std::vector<ThreadStats> thread_stats(config.num_threads);
  std::atomic<bool> stop_flag(false);

  void *buffer = nullptr;
  int fd = -1;
  void *mapped_area = nullptr;
  bool used_numa_alloc = false;

  try {
    if (config.device_path.empty()) {
      // Allocate memory buffer
      if (numa_available_on_system && config.memory_numa_node >= 0) {
        buffer = numa_alloc_onnode(config.buffer_size, config.memory_numa_node);
        used_numa_alloc = (buffer != nullptr);
      } else {
        buffer = aligned_alloc(4096, config.buffer_size);
      }

      if (!buffer) {
        std::cerr << "Failed to allocate memory: " << strerror(errno)
                  << std::endl;
        return 1;
      }

      // Initialize buffer with some data
      std::memset(buffer, 'A', config.buffer_size);

      // Create reader and writer threads for memory
      for (int i = 0; i < num_readers; i++) {
        int reader_id = i * 2; // 生成偶数ID: 0, 2, 4, ...
        threads.emplace_back(reader_thread, buffer, config.buffer_size,
                             config.block_size, std::ref(stop_flag),
                             std::ref(thread_stats[i]), read_limiter.get(),
                             reader_id, config.cpu_workload_size,
                             config.numa_node, config.enable_numa, config.random_access);
      }

      // 为写线程分配奇数ID (1, 3, 5...)
      for (int i = 0; i < num_writers; i++) {
        int writer_id = i * 2 + 1; // 生成奇数ID: 1, 3, 5, ...
        threads.emplace_back(
            writer_thread, buffer, config.buffer_size, config.block_size,
            std::ref(stop_flag), std::ref(thread_stats[num_readers + i]),
            write_limiter.get(), writer_id, config.cpu_workload_size,
            config.numa_node, config.enable_numa, config.random_access);
      }
    } else {
      // Open the device
      fd = open(config.device_path.c_str(), O_RDWR | O_DIRECT);
      if (fd < 0) {
        std::cerr << "Failed to open device " << config.device_path << ": "
                  << strerror(errno) << std::endl;
        return 1;
      }

      if (config.use_mmap) {
        // Use mmap for device access
        mapped_area = mmap(NULL, config.buffer_size, PROT_READ | PROT_WRITE,
                           MAP_SHARED, fd, 0);
        if (mapped_area == MAP_FAILED) {
          std::cerr << "Failed to mmap device: " << strerror(errno)
                    << std::endl;
          close(fd);
          return 1;
        }

        // Create reader and writer threads for mmap
        // 为读线程分配偶数ID
        for (int i = 0; i < num_readers; i++) {
          int reader_id = i * 2; // 生成偶数ID: 0, 2, 4, ...
          threads.emplace_back(mmap_reader_thread, mapped_area,
                               config.buffer_size, config.block_size,
                               std::ref(stop_flag), std::ref(thread_stats[i]),
                               read_limiter.get(), reader_id, config.numa_node,
                               config.enable_numa, config.random_access);
        }

        // 为写线程分配奇数ID
        for (int i = 0; i < num_writers; i++) {
          int writer_id = i * 2 + 1; // 生成奇数ID: 1, 3, 5, ...
          threads.emplace_back(
              mmap_writer_thread, mapped_area, config.buffer_size,
              config.block_size, std::ref(stop_flag),
              std::ref(thread_stats[num_readers + i]), write_limiter.get(),
              writer_id, config.numa_node, config.enable_numa, config.random_access);
        }
      } else {
        // Use read/write for device access
        // 为读线程分配偶数ID
        for (int i = 0; i < num_readers; i++) {
          int reader_id = i * 2; // 生成偶数ID: 0, 2, 4, ...
          threads.emplace_back(device_reader_thread, fd, config.buffer_size,
                               config.block_size, std::ref(stop_flag),
                               std::ref(thread_stats[i]), read_limiter.get(),
                               reader_id, config.numa_node, config.enable_numa, config.random_access);
        }

        // 为写线程分配奇数ID
        for (int i = 0; i < num_writers; i++) {
          int writer_id = i * 2 + 1; // 生成奇数ID: 1, 3, 5, ...
          threads.emplace_back(device_writer_thread, fd, config.buffer_size,
                               config.block_size, std::ref(stop_flag),
                               std::ref(thread_stats[num_readers + i]),
                               write_limiter.get(), writer_id, config.numa_node,
                               config.enable_numa, config.random_access);
        }
      }
    }

    // Run the benchmark for the specified duration
    auto start_time = std::chrono::steady_clock::now();
    std::this_thread::sleep_for(std::chrono::seconds(config.duration));
    stop_flag.store(true, std::memory_order_relaxed);
    auto end_time = std::chrono::steady_clock::now();

    // Wait for all threads to finish
    for (auto &t : threads) {
      if (t.joinable()) {
        t.join();
      }
    }

    // Calculate total stats
    double elapsed_seconds =
        std::chrono::duration<double>(end_time - start_time).count();
    size_t total_read_bytes = 0;
    size_t total_read_ops = 0;
    size_t total_write_bytes = 0;
    size_t total_write_ops = 0;

    for (int i = 0; i < num_readers; i++) {
      total_read_bytes += thread_stats[i].bytes_processed;
      total_read_ops += thread_stats[i].operations;
    }

    for (int i = 0; i < num_writers; i++) {
      total_write_bytes += thread_stats[num_readers + i].bytes_processed;
      total_write_ops += thread_stats[num_readers + i].operations;
    }

    // Calculate metrics
    double read_bandwidth_mbps = 0.0;
    double read_iops = 0.0;
    double write_bandwidth_mbps = 0.0;
    double write_iops = 0.0;
    
    if (num_readers > 0) {
      read_bandwidth_mbps = (total_read_bytes / (1024.0 * 1024.0)) / elapsed_seconds;
      read_iops = total_read_ops / elapsed_seconds;
    }
    
    if (num_writers > 0) {
      write_bandwidth_mbps = (total_write_bytes / (1024.0 * 1024.0)) / elapsed_seconds;
      write_iops = total_write_ops / elapsed_seconds;
    }
    
    double total_bandwidth_mbps =
        ((total_read_bytes + total_write_bytes) / (1024.0 * 1024.0)) /
        elapsed_seconds;
    double total_iops = (total_read_ops + total_write_ops) / elapsed_seconds;

    // Output results
    if (config.json_output) {
      std::cout << "{\n";
      std::cout << "  \"test_duration\": " << elapsed_seconds << ",\n";
      std::cout << "  \"buffer_size\": " << config.buffer_size << ",\n";
      std::cout << "  \"block_size\": " << config.block_size << ",\n";
      std::cout << "  \"num_threads\": " << config.num_threads << ",\n";
      std::cout << "  \"read_ratio\": " << config.read_ratio << ",\n";
      std::cout << "  \"num_readers\": " << num_readers << ",\n";
      std::cout << "  \"num_writers\": " << num_writers << ",\n";
      std::cout << "  \"read_bandwidth_mbps\": " << read_bandwidth_mbps << ",\n";
      std::cout << "  \"read_iops\": " << read_iops << ",\n";
      std::cout << "  \"write_bandwidth_mbps\": " << write_bandwidth_mbps << ",\n";
      std::cout << "  \"write_iops\": " << write_iops << ",\n";
      std::cout << "  \"total_bandwidth_mbps\": " << total_bandwidth_mbps << ",\n";
      std::cout << "  \"total_iops\": " << total_iops << ",\n";
      std::cout << "  \"total_read_bytes\": " << total_read_bytes << ",\n";
      std::cout << "  \"total_write_bytes\": " << total_write_bytes << ",\n";
      std::cout << "  \"total_read_ops\": " << total_read_ops << ",\n";
      std::cout << "  \"total_write_ops\": " << total_write_ops << ",\n";
      std::cout << "  \"numa_node\": " << config.numa_node << ",\n";
      std::cout << "  \"enable_numa\": " << (config.enable_numa ? "true" : "false") << ",\n";
      std::cout << "  \"memory_numa_node\": " << config.memory_numa_node << ",\n";
      std::cout << "  \"used_numa_alloc\": " << (used_numa_alloc ? "true" : "false") << ",\n";
      std::cout << "  \"device_path\": \"" << config.device_path << "\",\n";
      std::cout << "  \"use_mmap\": " << (config.use_mmap ? "true" : "false") << ",\n";
      std::cout << "  \"is_cxl_mem\": " << (config.is_cxl_mem ? "true" : "false") << ",\n";
      std::cout << "  \"random_access\": " << (config.random_access ? "true" : "false") << "\n";
      std::cout << "}" << std::endl;
    } else {
      std::cout << "\n=== Results ===" << std::endl;
      std::cout << "Test duration: " << elapsed_seconds << " seconds" << std::endl;
      
      if (num_readers > 0) {
        std::cout << "Read bandwidth: " << read_bandwidth_mbps << " MB/s" << std::endl;
        std::cout << "Read IOPS: " << read_iops << " ops/s" << std::endl;
      }
      
      if (num_writers > 0) {
        std::cout << "Write bandwidth: " << write_bandwidth_mbps << " MB/s" << std::endl;
        std::cout << "Write IOPS: " << write_iops << " ops/s" << std::endl;
      }
      
      std::cout << "Total bandwidth: " << total_bandwidth_mbps << " MB/s" << std::endl;
      std::cout << "Total IOPS: " << total_iops << " ops/s" << std::endl;
    }

  } catch (const std::exception &e) {
    std::cerr << "Error: " << e.what() << std::endl;
  }

  // Clean up resources
  if (mapped_area && mapped_area != MAP_FAILED) {
    munmap(mapped_area, config.buffer_size);
  }

  if (fd >= 0) {
    close(fd);
  }

  if (buffer) {
    if (used_numa_alloc) {
      numa_free(buffer, config.buffer_size);
    } else {
      free(buffer);
    }
  }

  return 0;
}
