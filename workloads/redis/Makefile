workloads/redis/Makefile.PHONY: all build build-redis build-tools configure benchmark clean install deps test

# Redis configuration
REDIS_DIR = redis-src
BUILD_THREADS = $(shell nproc)
REDIS_SERVER = $(REDIS_DIR)/src/redis-server
REDIS_CLI = $(REDIS_DIR)/src/redis-cli
REDIS_BENCHMARK = $(REDIS_DIR)/src/redis-benchmark

# Check if submodule is initialized
CHECK_SUBMODULE = @if [ ! -f "$(REDIS_DIR)/Makefile" ]; then \
	echo "Error: Redis submodule not initialized. Run 'git submodule update --init' first"; \
	exit 1; \
fi

all: deps build configure

deps:
	@echo "Installing Redis dependencies..."
	sudo apt-get update
	sudo apt-get install -y build-essential tcl pkg-config libsystemd-dev \
		autoconf automake libpcre2-dev libevent-dev zlib1g-dev libssl-dev

# Initialize and update submodule
init-submodule:
	@echo "Initializing Redis submodule..."
	git submodule update --init --recursive

# Main build target
build: build-redis build-tools build-memtier

# Build Redis server and core components
build-redis: 
	$(CHECK_SUBMODULE)
	@echo "Building Redis from submodule..."
	@echo "Fixing hdr_histogram for redis-benchmark..."
	@cd $(REDIS_DIR)/deps/hdr_histogram && \
		$(MAKE) clean 2>/dev/null || true && \
		$(MAKE)
	cd $(REDIS_DIR) && make -j$(BUILD_THREADS) BUILD_TLS=yes
	@echo "Redis binaries built successfully:"
	@ls -la $(REDIS_SERVER) $(REDIS_CLI) $(REDIS_BENCHMARK) 2>/dev/null || true

# Build specific Redis tools
build-tools:
	$(CHECK_SUBMODULE)
	@echo "Building Redis tools..."
	cd $(REDIS_DIR) && make redis-benchmark redis-cli redis-check-aof redis-check-rdb

# Build memtier_benchmark tool
build-memtier:
	@echo "Building memtier_benchmark..."
	@if [ ! -f "memtier_benchmark/configure" ]; then \
		cd memtier_benchmark && autoreconf -ivf; \
	fi
	@if [ ! -f "memtier_benchmark/Makefile" ]; then \
		cd memtier_benchmark && ./configure; \
	fi
	cd memtier_benchmark && make -j$(BUILD_THREADS)
	@echo "memtier_benchmark built successfully:"
	@ls -la memtier_benchmark/memtier_benchmark 2>/dev/null || true

# Build with specific allocator
build-jemalloc:
	$(CHECK_SUBMODULE)
	@echo "Building Redis with jemalloc..."
	cd $(REDIS_DIR) && make MALLOC=jemalloc -j$(BUILD_THREADS)

build-libc:
	$(CHECK_SUBMODULE)
	@echo "Building Redis with libc malloc..."
	cd $(REDIS_DIR) && make MALLOC=libc -j$(BUILD_THREADS)

# Build 32-bit version
build-32bit:
	$(CHECK_SUBMODULE)
	@echo "Building 32-bit Redis..."
	cd $(REDIS_DIR) && make 32bit -j$(BUILD_THREADS)

# Build with debugging symbols
build-debug:
	$(CHECK_SUBMODULE)
	@echo "Building Redis with debugging symbols..."
	cd $(REDIS_DIR) && make noopt -j$(BUILD_THREADS)

configure:
	@echo "Configuring Redis..."
	@if [ -f "$(REDIS_SERVER)" ]; then \
		cp redis.conf $(REDIS_DIR)/redis.conf; \
		echo "Redis configuration applied"; \
	else \
		echo "Warning: Redis not built yet. Run 'make build' first"; \
	fi

install: build
	@echo "Installing Redis system-wide..."
	cd $(REDIS_DIR) && sudo make install

start:
	@echo "Starting Redis server..."
	@if [ ! -f "$(REDIS_SERVER)" ]; then \
		echo "Error: Redis not built. Run 'make build' first"; \
		exit 1; \
	fi
	$(REDIS_SERVER) redis.conf &
	@sleep 2
	@echo "Redis server started. PID: $$(pgrep -f redis-server)"

stop:
	@echo "Stopping Redis server..."
	@if [ -f "$(REDIS_CLI)" ]; then \
		$(REDIS_CLI) shutdown 2>/dev/null || true; \
	fi
	@pkill -f redis-server 2>/dev/null || true
	@echo "Redis server stopped"

restart: stop start

# Check Redis server status
status:
	@if [ -f "$(REDIS_CLI)" ]; then \
		echo "Checking Redis status..."; \
		$(REDIS_CLI) ping 2>/dev/null && echo "Redis is running" || echo "Redis is not running"; \
	else \
		echo "Redis CLI not found. Build Redis first with 'make build'"; \
	fi

# Benchmark targets
benchmark: build start
	@echo "Running comprehensive Redis benchmark..."
	python3 redis_benchmark.py
	@make stop

quick-benchmark: build start
	@echo "Running quick Redis benchmark..."
	$(REDIS_BENCHMARK) -t set,get -n 100000 -c 50 -P 16
	@make stop

# Advanced benchmark options
benchmark-latency: build start
	@echo "Running latency-focused benchmark..."
	$(REDIS_BENCHMARK) -t get,set -n 100000 -c 1 -P 1 --csv
	@make stop

benchmark-throughput: build start
	@echo "Running throughput-focused benchmark..."
	$(REDIS_BENCHMARK) -t get,set -n 1000000 -c 100 -P 32 --csv
	@make stop

benchmark-pipeline: build start
	@echo "Testing pipeline performance..."
	@for p in 1 8 16 32 64 128; do \
		echo "Pipeline size: $$p"; \
		$(REDIS_BENCHMARK) -t set -n 100000 -c 50 -P $$p -q; \
	done
	@make stop

# Memory benchmark
benchmark-memory: build start
	@echo "Running memory benchmark with different data sizes..."
	@for size in 10 100 1000 10000; do \
		echo "Data size: $$size bytes"; \
		$(REDIS_BENCHMARK) -t set,get -n 10000 -d $$size -q; \
	done
	@make stop

# Clean targets
clean:
	@echo "Cleaning Redis build..."
	@if [ -d "$(REDIS_DIR)" ]; then \
		cd $(REDIS_DIR) && make clean; \
	fi
	@if [ -d "memtier_benchmark" ] && [ -f "memtier_benchmark/Makefile" ]; then \
		cd memtier_benchmark && make clean; \
	fi
	@rm -f dump.rdb appendonly.aof
	@echo "Clean complete"

distclean: clean
	@echo "Removing all generated files..."
	@rm -rf results/
	@rm -f *.json *.log
	@echo "Distribution clean complete"

# Test targets
test: build
	@echo "Running Redis unit tests..."
	cd $(REDIS_DIR) && make test

test-quick: build
	@echo "Running quick Redis tests..."
	cd $(REDIS_DIR) && make test-unit

# Validation
validate:
	@echo "Validating Redis build..."
	@if [ -f "$(REDIS_SERVER)" ]; then \
		echo "✓ redis-server found"; \
	else \
		echo "✗ redis-server not found"; \
	fi
	@if [ -f "$(REDIS_CLI)" ]; then \
		echo "✓ redis-cli found"; \
	else \
		echo "✗ redis-cli not found"; \
	fi
	@if [ -f "$(REDIS_BENCHMARK)" ]; then \
		echo "✓ redis-benchmark found"; \
	else \
		echo "✗ redis-benchmark not found"; \
	fi
	@if [ -f "$(REDIS_SERVER)" ]; then \
		echo "Checking Redis version..."; \
		$(REDIS_SERVER) --version || echo "Failed to get Redis version"; \
	fi

# Help target
help:
	@echo "Redis Build and Benchmark Makefile"
	@echo "=================================="
	@echo "Build targets:"
	@echo "  make build          - Build Redis from submodule"
	@echo "  make build-memtier  - Build memtier_benchmark tool"
	@echo "  make build-debug    - Build with debugging symbols"
	@echo "  make build-jemalloc - Build with jemalloc allocator"
	@echo "  make build-libc     - Build with libc malloc"
	@echo "  make build-32bit    - Build 32-bit version"
	@echo ""
	@echo "Runtime targets:"
	@echo "  make start          - Start Redis server"
	@echo "  make stop           - Stop Redis server"
	@echo "  make restart        - Restart Redis server"
	@echo "  make status         - Check Redis status"
	@echo ""
	@echo "Benchmark targets:"
	@echo "  make benchmark           - Run comprehensive benchmark"
	@echo "  make quick-benchmark     - Run quick benchmark"
	@echo "  make benchmark-latency   - Focus on latency testing"
	@echo "  make benchmark-throughput - Focus on throughput testing"
	@echo "  make benchmark-pipeline  - Test pipeline performance"
	@echo "  make benchmark-memory    - Test with different data sizes"
	@echo ""
	@echo "Other targets:"
	@echo "  make test           - Run Redis unit tests"
	@echo "  make clean          - Clean build artifacts"
	@echo "  make distclean      - Remove all generated files"
	@echo "  make validate       - Validate Redis build"
	@echo "  make help           - Show this help message"

.DEFAULT_GOAL := help
