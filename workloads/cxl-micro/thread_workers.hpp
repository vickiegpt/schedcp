/**
 * thread_workers.hpp - Header-only library for CXL bandwidth benchmark thread workers
 *
 * This header contains all thread worker functions for the CXL double bandwidth
 * microbenchmark, including memory-based and device-based reader/writer threads.
 */

#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <mutex>
#include <numa.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <thread>
#include <unistd.h>
#include <vector>
#include <random>

#include "../../include/schedcp_slug_marker.h"

// Forward declarations for types used in thread functions
struct ThreadStats {
  size_t bytes_processed = 0;
  size_t operations = 0;
  int thread_id = 0;
  size_t cpu_hash = 0;
};

// Rate limiter class for bandwidth control
class RateLimiter {
private:
  std::mutex mutex_;
  size_t tokens_;
  size_t max_tokens_;
  size_t tokens_per_second_;
  std::chrono::steady_clock::time_point last_refill_;

public:
  RateLimiter(size_t max_bandwidth_mbps)
      : tokens_(0), max_tokens_(0), tokens_per_second_(0) {
    if (max_bandwidth_mbps > 0) {
      max_tokens_ = max_bandwidth_mbps * 1024 * 1024;
      tokens_per_second_ = max_tokens_;
      tokens_ = max_tokens_;
    }
    last_refill_ = std::chrono::steady_clock::now();
  }

  bool acquire(size_t bytes) {
    if (tokens_per_second_ == 0) {
      return true;
    }

    std::lock_guard<std::mutex> lock(mutex_);

    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        now - last_refill_);

    if (elapsed.count() > 0) {
      size_t new_tokens = (tokens_per_second_ * elapsed.count()) / 1000000;
      tokens_ = std::min(tokens_ + new_tokens, max_tokens_);
      last_refill_ = now;
    }

    if (tokens_ >= bytes) {
      tokens_ -= bytes;
      return true;
    }

    return false;
  }

  void wait_for_tokens(size_t bytes) {
    if (tokens_per_second_ == 0) {
      return;
    }

    while (!acquire(bytes)) {
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
  }
};

// Helper function for getting Linux thread ID
inline pid_t gettid() { 
  return syscall(SYS_gettid); 
}

inline void warn_numa_bind_failure_once(std::atomic<bool> &warned,
                                        const char *worker_type,
                                        int thread_id, int numa_node) {
  if (!warned.exchange(true)) {
    std::cerr << "Warning: Failed to bind " << worker_type << " thread "
              << thread_id << " to NUMA node " << numa_node << ": "
              << strerror(errno) << " (further failures suppressed)"
              << std::endl;
  }
}

// Memory-based reader thread
inline void reader_thread(void *buffer, size_t buffer_size, size_t block_size,
                   std::atomic<bool> &stop_flag, ThreadStats &stats,
                   RateLimiter *rate_limiter, int thread_id,
                   size_t cpu_workload_size, int numa_node, bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size);
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (buffer_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "reader", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_READ_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    std::memcpy(local_buffer.data(), static_cast<char *>(buffer) + offset,
                block_size);

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (buffer_size - block_size);
    }

    stats.bytes_processed += block_size;
    stats.operations++;

    if (cpu_workload_size > 0) {
      size_t work = std::min(cpu_workload_size, block_size);
      size_t hash_val = 0;
      for (size_t j = 0; j < work; ++j) {
        hash_val = hash_val * 1315423911ull +
                   static_cast<unsigned char>(local_buffer[j]);
      }
      stats.cpu_hash ^= hash_val;
    }
  }
}

// Memory-based writer thread
inline void writer_thread(void *buffer, size_t buffer_size, size_t block_size,
                   std::atomic<bool> &stop_flag, ThreadStats &stats,
                   RateLimiter *rate_limiter, int thread_id,
                   size_t cpu_workload_size, int numa_node, bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size, 'W');
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (buffer_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "writer", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_WRITE_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    std::memcpy(static_cast<char *>(buffer) + offset, local_buffer.data(),
                block_size);

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (buffer_size - block_size);
    }

    stats.bytes_processed += block_size;
    stats.operations++;

    if (cpu_workload_size > 0) {
      size_t work = std::min(cpu_workload_size, block_size);
      size_t hash_val = 0;
      for (size_t j = 0; j < work; ++j) {
        hash_val = hash_val * 1315423911ull +
                   static_cast<unsigned char>(local_buffer[j]);
      }
      stats.cpu_hash ^= hash_val;
    }
  }
}

// Device-based reader thread using read() syscall
inline void device_reader_thread(int fd, size_t file_size, size_t block_size,
                          std::atomic<bool> &stop_flag, ThreadStats &stats,
                          RateLimiter *rate_limiter, int thread_id,
                          int numa_node, bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size);
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (file_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "device reader", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_READ_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    lseek(fd, offset, SEEK_SET);

    ssize_t bytes_read = read(fd, local_buffer.data(), block_size);
    if (bytes_read <= 0) {
      std::cerr << "Error reading from device: " << strerror(errno)
                << std::endl;
      break;
    }

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (file_size - block_size);
    }

    stats.bytes_processed += bytes_read;
    stats.operations++;
  }
}

// Device-based writer thread using write() syscall
inline void device_writer_thread(int fd, size_t file_size, size_t block_size,
                          std::atomic<bool> &stop_flag, ThreadStats &stats,
                          RateLimiter *rate_limiter, int thread_id,
                          int numa_node, bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size, 'W');
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (file_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "device writer", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_WRITE_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    lseek(fd, offset, SEEK_SET);

    ssize_t bytes_written = write(fd, local_buffer.data(), block_size);
    if (bytes_written <= 0) {
      std::cerr << "Error writing to device: " << strerror(errno) << std::endl;
      break;
    }

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (file_size - block_size);
    }

    stats.bytes_processed += bytes_written;
    stats.operations++;
  }
}

// Memory-mapped reader thread
inline void mmap_reader_thread(void *mapped_area, size_t file_size, size_t block_size,
                        std::atomic<bool> &stop_flag, ThreadStats &stats,
                        RateLimiter *rate_limiter, int thread_id, int numa_node,
                        bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size);
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (file_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "MMAP reader", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_READ_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    std::memcpy(local_buffer.data(), static_cast<char *>(mapped_area) + offset,
                block_size);

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (file_size - block_size);
    }

    stats.bytes_processed += block_size;
    stats.operations++;
  }
}

// Memory-mapped writer thread
inline void mmap_writer_thread(void *mapped_area, size_t file_size, size_t block_size,
                        std::atomic<bool> &stop_flag, ThreadStats &stats,
                        RateLimiter *rate_limiter, int thread_id, int numa_node,
                        bool enable_numa, bool random_access) {
  std::vector<char> local_buffer(block_size, 'W');
  size_t offset = 0;
  
  // Random number generator for random access pattern
  std::random_device rd;
  std::mt19937 gen(rd() + thread_id); // Seed with random device + thread_id for uniqueness
  std::uniform_int_distribution<size_t> dis(0, (file_size - block_size) / block_size - 1);

  stats.thread_id = thread_id;

  if (enable_numa) {
    static std::atomic<bool> warned(false);
    if (numa_run_on_node(numa_node) != 0) {
      warn_numa_bind_failure_once(warned, "MMAP writer", thread_id, numa_node);
    }
  }

  // Thread started silently
  SLUG_MARK_WRITE_BB();

  while (!stop_flag.load(std::memory_order_relaxed)) {
    if (rate_limiter) {
      rate_limiter->wait_for_tokens(block_size);
    }

    std::memcpy(static_cast<char *>(mapped_area) + offset, local_buffer.data(),
                block_size);

    if (random_access) {
      offset = dis(gen) * block_size;
    } else {
      offset = (offset + block_size) % (file_size - block_size);
    }

    stats.bytes_processed += block_size;
    stats.operations++;
  }
}
