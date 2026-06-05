.PHONY: all clone build configure benchmark clean install deps tpcc-query-bench

# RocksDB configuration
ROCKSDB_REPO = https://github.com/facebook/rocksdb.git
ROCKSDB_DIR = rocksdb
ROCKSDB_BRANCH = main
BUILD_THREADS = $(shell nproc)
ROCKSDB_BUILD_ENV = CCACHE_DISABLE=1 DISABLE_WARNING_AS_ERROR=1 DEBUG_LEVEL=0 LIB_MODE=static
TPCC_BENCH = tpcc_query_bench
TPCC_SRC = tpcc_query_bench.cpp
TPCC_LIBS = -lgflags -lz -lzstd -lnuma -ltbb -luring -ldl -lrt -pthread -latomic

all: deps clone build configure

deps:
	@echo "Installing RocksDB dependencies..."
	sudo apt-get update
	sudo apt-get install -y build-essential libgflags-dev libsnappy-dev \
		zlib1g-dev libbz2-dev liblz4-dev libzstd-dev cmake \
		libgtest-dev libtbb-dev

clone:
	@echo "Cloning RocksDB repository..."
	if [ ! -d $(ROCKSDB_DIR) ]; then \
		git clone --depth=1 --branch=$(ROCKSDB_BRANCH) $(ROCKSDB_REPO) $(ROCKSDB_DIR); \
	fi

build: clone
	@echo "Building RocksDB..."
	cd $(ROCKSDB_DIR) && $(ROCKSDB_BUILD_ENV) make -j$(BUILD_THREADS) static_lib
	cd $(ROCKSDB_DIR) && $(ROCKSDB_BUILD_ENV) make -j$(BUILD_THREADS) db_bench

tpcc-query-bench: build $(TPCC_SRC)
	@echo "Building RocksDB TPCC-like query benchmark..."
	$(CXX) -O3 -std=c++20 -I$(ROCKSDB_DIR)/include -I../../include \
		$(TPCC_SRC) $(ROCKSDB_DIR)/librocksdb.a $(TPCC_LIBS) \
		-o $(TPCC_BENCH)

configure:
	@echo "Configuring RocksDB..."
	mkdir -p /tmp/rocksdb_data

benchmark:
	@echo "Running RocksDB benchmark..."
	python3 rocksdb_benchmark.py

quick-benchmark:
	@echo "Running quick RocksDB benchmark..."
	cd $(ROCKSDB_DIR) && ./db_bench \
		--benchmarks=fillseq,readrandom \
		--num=100000 \
		--db=/tmp/rocksdb_data \
		--disable_wal=true \
		--statistics=false

clean:
	@echo "Cleaning up..."
	cd $(ROCKSDB_DIR) && make clean || true
	rm -rf /tmp/rocksdb_data
	rm -rf $(ROCKSDB_DIR)

install: build
	@echo "Installing RocksDB..."
	cd $(ROCKSDB_DIR) && sudo make install

test: build
	@echo "Running RocksDB tests..."
	cd $(ROCKSDB_DIR) && make -j$(BUILD_THREADS) check
