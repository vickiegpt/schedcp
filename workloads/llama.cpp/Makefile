.PHONY: all build build-llama build-cuda clean update help download-models test-quick test-full test-sharegpt bench-schedulers

BUILD_DIR := build
BUILD_STAMP := $(BUILD_DIR)/.dir
LLAMA_DIR := llama.cpp
BINARY := $(BUILD_DIR)/llama-cli

all: build

$(BUILD_STAMP):
	@mkdir -p $(BUILD_DIR)
	@touch $(BUILD_STAMP)

build: $(BUILD_STAMP)
	@echo "Building llama.cpp..."
	@cd $(LLAMA_DIR) && cmake -B ../$(BUILD_DIR) . \
		-DCMAKE_BUILD_TYPE=Release \
		-DLLAMA_CURL=OFF \
		-DGGML_CCACHE=OFF \
		-DGGML_CUDA=OFF \
		-DGGML_METAL=OFF \
		-DGGML_VULKAN=OFF \
		-DGGML_SYCL=OFF
	@cd $(BUILD_DIR) && make -j$(shell nproc)
	@echo "Build complete. Binary available at: $(BINARY)"

# Flexible build target matching build_llama.sh
# Usage: make build-llama [BUILD_TYPE=Release|Debug] [CUDA=ON|OFF] [METAL=ON|OFF] [VULKAN=ON|OFF] [JOBS=N]
BUILD_TYPE ?= Release
CUDA ?= OFF
METAL ?= OFF
VULKAN ?= OFF
JOBS ?= $(shell nproc)

build-llama: $(BUILD_STAMP)
	@echo "Building llama.cpp..."
	@echo "  Build type: $(BUILD_TYPE)"
	@echo "  CUDA support: $(CUDA)"
	@echo "  Metal support: $(METAL)"
	@echo "  Vulkan support: $(VULKAN)"
	@echo "  Parallel jobs: $(JOBS)"
	@cd $(LLAMA_DIR) && cmake -B ../$(BUILD_DIR) . \
		-DCMAKE_BUILD_TYPE=$(BUILD_TYPE) \
		-DLLAMA_CURL=OFF \
		-DGGML_CCACHE=OFF \
		-DGGML_CUDA=$(CUDA) \
		-DGGML_METAL=$(METAL) \
		-DGGML_VULKAN=$(VULKAN) \
		-DGGML_SYCL=OFF
	@cd $(BUILD_DIR) && make -j$(JOBS)
	@echo "Build complete!"
	@echo "Binaries available in: $(BUILD_DIR)"
	@if [ -f "$(BUILD_DIR)/bin/llama-cli" ]; then \
		echo "Testing binary..."; \
		$(BUILD_DIR)/bin/llama-cli --version; \
	elif [ -f "$(BINARY)" ]; then \
		echo "Testing binary..."; \
		$(BINARY) --version; \
	fi

# CUDA build target matching build_cuda.sh (using GCC-12 for compatibility)
build-cuda:
	@echo "Building llama.cpp with CUDA support..."
	@echo "Note: Using GCC-12 to avoid compatibility issues with CUDA 12.9"
	@rm -rf $(BUILD_DIR)
	@mkdir -p $(BUILD_DIR)
	@cd $(LLAMA_DIR) && CC=gcc-12 CXX=g++-12 CMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc \
		cmake -B ../$(BUILD_DIR) . \
		-DCMAKE_BUILD_TYPE=Release \
		-DLLAMA_CURL=OFF \
		-DGGML_CCACHE=OFF \
		-DGGML_CUDA=ON \
		-DGGML_METAL=OFF \
		-DGGML_VULKAN=OFF \
		-DGGML_SYCL=OFF \
		-DCMAKE_CUDA_HOST_COMPILER=g++-12 \
		-DCMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc
	@cd $(BUILD_DIR) && make -j$(shell nproc)
	@echo ""
	@echo "Build complete!"
	@echo "Binaries available in: $(BUILD_DIR)/bin/"
	@echo ""
	@echo "Testing CUDA support..."
	@if [ -f "$(BUILD_DIR)/bin/llama-cli" ]; then \
		$(BUILD_DIR)/bin/llama-cli --version; \
	fi
	@echo ""
	@echo "Built binaries:"
	@ls -lh $(BUILD_DIR)/bin/ 2>/dev/null | grep -E '^-rwx' || true

# CUDA build without VMM (for UVM compatibility)
build-cuda-no-vmm:
	@echo "Building llama.cpp with CUDA support (VMM DISABLED for UVM)..."
	@echo "Note: Using GCC-12 to avoid compatibility issues with CUDA 12.9"
	@rm -rf $(BUILD_DIR)
	@mkdir -p $(BUILD_DIR)
	@cd $(LLAMA_DIR) && CC=gcc-12 CXX=g++-12 CMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc \
		cmake -B ../$(BUILD_DIR) . \
		-DCMAKE_BUILD_TYPE=Release \
		-DLLAMA_CURL=OFF \
		-DGGML_CCACHE=OFF \
		-DGGML_CUDA=ON \
		-DGGML_CUDA_NO_VMM=ON \
		-DCMAKE_CUDA_HOST_COMPILER=g++-12 \
		-DCMAKE_CUDA_COMPILER=/usr/local/cuda-12.9/bin/nvcc
	@cd $(BUILD_DIR) && make -j$(shell nproc)
	@echo ""
	@echo "Build complete (NO VMM)!"
	@echo "Binaries available in: $(BUILD_DIR)/bin/"
	@echo ""
	@echo "Testing CUDA support..."
	@if [ -f "$(BUILD_DIR)/bin/llama-cli" ]; then \
		$(BUILD_DIR)/bin/llama-cli --version; \
	fi
	@echo ""
	@echo "Built binaries:"
	@ls -lh $(BUILD_DIR)/bin/ 2>/dev/null | grep -E '^-rwx' || true

clean:
	@echo "Cleaning build artifacts..."
	@rm -rf $(BUILD_DIR)
	@echo "Clean complete."

update:
	@echo "Updating llama.cpp submodule..."
	@git submodule update --init --recursive
	@cd $(LLAMA_DIR) && git pull origin master
	@echo "Update complete."

download-models: $(MODEL_DIR)
	@echo "Checking models..."
	@if [ ! -f "$(MODEL_1B)" ]; then \
		echo "Downloading TinyLlama 1.1B model..."; \
		python download_test_model.py; \
	fi
	@if [ ! -f "$(MODEL_3B)" ]; then \
		echo "Downloading Llama 3.2 3B model..."; \
		cd $(MODEL_DIR) && huggingface-cli download bartowski/Llama-3.2-3B-Instruct-GGUF --include "Llama-3.2-3B-Instruct-Q4_K_M.gguf" --local-dir .; \
	fi
	@echo "Models ready."

$(MODEL_DIR):
	@mkdir -p $(MODEL_DIR)

$(DATASET_DIR):
	@mkdir -p $(DATASET_DIR)

$(RESULTS_DIR):
	@mkdir -p $(RESULTS_DIR)

test-quick: build download-models $(DATASET_DIR) $(RESULTS_DIR)
	@echo "Running quick ShareGPT test ($(SAMPLES_QUICK) samples, $(CONCURRENT_QUICK) concurrent)..."
	@if [ ! -f "$(SHAREGPT_VICUNA)" ] && [ ! -f "$(SHAREGPT_BENCH)" ]; then \
		echo "Downloading ShareGPT dataset..."; \
		python download_sharegpt.py --dataset vicuna --num-samples 100; \
	fi
	@python sharegpt_llama_server_eval.py \
		--num-samples $(SAMPLES_QUICK) \
		--max-concurrent $(CONCURRENT_QUICK) \
		--server-logs

test-full: build download-models $(DATASET_DIR) $(RESULTS_DIR)
	@echo "Running full ShareGPT test ($(SAMPLES_FULL) samples, $(CONCURRENT_FULL) concurrent)..."
	@if [ ! -f "$(SHAREGPT_VICUNA)" ] && [ ! -f "$(SHAREGPT_BENCH)" ]; then \
		echo "Downloading ShareGPT dataset..."; \
		python download_sharegpt.py --dataset vicuna --num-samples 500; \
	fi
	@python sharegpt_llama_server_eval.py \
		--num-samples $(SAMPLES_FULL) \
		--max-concurrent $(CONCURRENT_FULL) \
		--n-threads 32 \
		--n-parallel 32 \
		--server-logs

test-sharegpt: test-quick
	@echo "ShareGPT quick test completed. Check results directory."

bench-schedulers: build download-models $(DATASET_DIR) $(RESULTS_DIR)
	@echo "Benchmarking all production schedulers..."
	@if [ ! -f "$(SHAREGPT_VICUNA)" ] && [ ! -f "$(SHAREGPT_BENCH)" ]; then \
		echo "Downloading ShareGPT dataset..."; \
		python download_sharegpt.py --dataset vicuna --num-samples 100; \
	fi
	@python sharegpt_llama_server_eval.py \
		--num-samples 20 \
		--max-concurrent 64 \
		--server-logs

# Custom benchmark with specific parameters
bench-custom: build download-models $(DATASET_DIR) $(RESULTS_DIR)
	@echo "Custom benchmark (set SAMPLES and CONCURRENT env vars)..."
	@python sharegpt_llama_server_eval.py \
		--num-samples $${SAMPLES:-10} \
		--max-concurrent $${CONCURRENT:-2} \
		--server-logs

# Run 1B model (TinyLlama) for quick testing
run-1b: build download-models
	@echo "Running TinyLlama 1.1B model..."
	@$(BUILD_DIR)/bin/llama-cli \
		-m $(MODEL_1B) \
		-p "You are a helpful assistant" \
		-n 256 \
		--interactive-first

# Benchmark with 1B model
bench-1b: build download-models $(DATASET_DIR) $(RESULTS_DIR)
	@echo "Benchmarking with TinyLlama 1.1B model..."
	@if [ ! -f "$(SHAREGPT_VICUNA)" ] && [ ! -f "$(SHAREGPT_BENCH)" ]; then \
		echo "Downloading ShareGPT dataset..."; \
		python download_sharegpt.py --dataset vicuna --num-samples 100; \
	fi
	@python sharegpt_llama_server_eval.py \
		--model $(MODEL_1B) \
		--num-samples 10 \
		--max-concurrent 4 \
		--server-logs

# Benchmark GPT-OSS-120B with UVM (60GB model on 32GB GPU)
bench-120b-uvm: build-cuda
	@echo "=== Benchmarking GPT-OSS-120B with UVM ==="
	@echo "Model: GPT-OSS-120B (116.83B params, 59.02 GiB)"
	@echo "GPU: RTX 5090 (32GB VRAM)"
	@echo "Method: CUDA Unified Memory with CPU-preferred location"
	@echo ""
	@echo "Running llama-bench with UVM enabled..."
	@mkdir -p $(RESULTS_DIR)
	GGML_CUDA_ENABLE_UNIFIED_MEMORY=1 $(BUILD_DIR)/bin/llama-bench \
		-m $(MODEL_120B_CACHE) \
		2>&1 | tee $(RESULTS_DIR)/gpt-oss-120b-uvm-bench.log
	@echo ""
	@echo "Benchmark complete! Results saved to: $(RESULTS_DIR)/gpt-oss-120b-uvm-bench.log"
	@echo ""
	@echo "Key metrics:"
	@grep -E "pp |tg " $(RESULTS_DIR)/gpt-oss-120b-uvm-bench.log || true

# Run GPT-OSS-120B server with UVM
run-120b-uvm: build-cuda
	@echo "=== Starting GPT-OSS-120B Server with UVM ==="
	@echo "Model: GPT-OSS-120B (116.83B params, 59.02 GiB)"
	@echo "Context: 4096 tokens (reduced from default 262144)"
	@echo "Total memory: ~60GB (model 59GB + KV cache 288MB)"
	@echo ""
	GGML_CUDA_ENABLE_UNIFIED_MEMORY=1 $(BUILD_DIR)/bin/llama-server \
		--gpt-oss-120b-default 

# Quick test GPT-OSS-120B with UVM (single inference)
test-120b-uvm: build-cuda
	@echo "=== Quick Test: GPT-OSS-120B with UVM ==="
	@echo "Running single inference to verify UVM is working..."
	@echo ""
	GGML_CUDA_ENABLE_UNIFIED_MEMORY=1 $(BUILD_DIR)/bin/llama-cli \
		-m $(MODEL_120B_CACHE) \
		-p "Explain what unified memory is in CUDA:" \
		-n 256 \
		-ngl 37 \
		-c 4096 \
		--log-disable

# Model and dataset paths
MODEL_DIR := models
DATASET_DIR := datasets
RESULTS_DIR := results
LLAMA_SERVER := $(BUILD_DIR)/bin/llama-server
MODEL_3B := $(MODEL_DIR)/Llama-3.2-3B-Instruct-Q4_K_M.gguf
MODEL_1B := $(MODEL_DIR)/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf
MODEL_120B_CACHE := /home/yunwei37/.cache/llama.cpp/ggml-org_gpt-oss-120b-GGUF_gpt-oss-120b-mxfp4-00001-of-00003.gguf
SHAREGPT_VICUNA := $(DATASET_DIR)/sharegpt_vicuna.json
SHAREGPT_BENCH := $(DATASET_DIR)/sharegpt_benchmark.json

# Benchmark parameters
SAMPLES_QUICK := 30
SAMPLES_FULL := 100
CONCURRENT_QUICK := 4
CONCURRENT_FULL := 128

help:
	@echo "Available targets:"
	@echo "  Build targets:"
	@echo "    make build        - Build llama.cpp (CPU only)"
	@echo "    make build-cuda   - Build llama.cpp with CUDA support (GCC-12, CUDA 12.9)"
	@echo "    make build-llama  - Flexible build with options:"
	@echo "                        BUILD_TYPE=Release|Debug (default: Release)"
	@echo "                        CUDA=ON|OFF (default: OFF)"
	@echo "                        METAL=ON|OFF (default: OFF)"
	@echo "                        VULKAN=ON|OFF (default: OFF)"
	@echo "                        JOBS=N (default: nproc)"
	@echo "                        Example: make build-llama CUDA=ON BUILD_TYPE=Debug JOBS=8"
	@echo "  "
	@echo "  Setup targets:"
	@echo "    make download-models - Download all required models"
	@echo "  "
	@echo "  Run targets:"
	@echo "    make run-1b       - Run TinyLlama 1.1B model interactively"
	@echo "    make run-120b-uvm - Run GPT-OSS-120B server with UVM (60GB on 32GB GPU)"
	@echo "  "
	@echo "  Benchmark targets:"
	@echo "    make test-quick   - Quick test with 5 samples, 2 concurrent"
	@echo "    make test-full    - Full test with 100 samples, 10 concurrent"
	@echo "    make test-sharegpt - Test ShareGPT server benchmark"
	@echo "    make bench-schedulers - Benchmark all production schedulers"
	@echo "    make bench-1b     - Benchmark with TinyLlama 1.1B model"
	@echo "    make bench-120b-uvm - Benchmark GPT-OSS-120B with UVM (3 runs)"
	@echo "    make test-120b-uvm  - Quick test GPT-OSS-120B with UVM (single inference)"
	@echo "  "
	@echo "  Maintenance targets:"
	@echo "    make clean        - Remove build artifacts"
	@echo "    make update       - Update llama.cpp submodule"
	@echo "    make help         - Show this help message"
	@echo "  "
	@echo "UVM (Unified Virtual Memory) enables running models larger than GPU VRAM"
	@echo "by paging to system RAM. See UVM_FIX_SUMMARY.md for details."
