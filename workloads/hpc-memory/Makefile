.PHONY: all build benchmark clean

CXX ?= g++
CXXFLAGS ?= -O3 -std=c++17 -march=native -fopenmp -Wall -Wextra
LDFLAGS ?= -fopenmp

BIN := hpc_memory_kernels

all: build

build: $(BIN)

$(BIN): hpc_memory_kernels.cpp ../../include/schedcp_slug_marker.h
	$(CXX) $(CXXFLAGS) -I../../include $< $(LDFLAGS) -o $@

benchmark: build
	python3 hpc_memory_benchmark.py --quick

clean:
	rm -f $(BIN)
