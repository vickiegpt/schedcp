# Makefile for building SCX schedulers

.PHONY: all build build-c build-rust build-tools check-scx-submodule clean help update doc deps install-deps install

MESON_CC ?= $(shell command -v gcc 2>/dev/null || command -v cc)
BPFTOOL ?= $(shell command -v bpftool 2>/dev/null || echo bpftool)
SCX_C_BUILD_DIR ?= build-c
SCX_BUILD_DIR ?= build
MESON_SETUP_BASE = CC=$(MESON_CC) meson setup
MESON_SETUP_FLAGS = --prefix=~ -Dbpftool=$(BPFTOOL) -Dlibbpf_a=disabled

# Default target
all: build build-tools

# Build all SCX schedulers (C and Rust)
build: build-c build-rust
	@echo "All schedulers built successfully!"
	@echo "Listing all compiled schedulers:"
	@ls -la sche_bin/ 2>/dev/null || echo "No schedulers found in sche_bin/"

check-scx-submodule:
	@if [ ! -f scx/meson.build ]; then \
		echo "SCX submodule is not initialized."; \
		echo "Run: git submodule update --init --recursive scheduler/scx"; \
		exit 1; \
	fi

# Build C schedulers only
build-c: check-scx-submodule
	@echo "Building C SCX schedulers..."
	@mkdir -p sche_bin
	@cd scx && \
	echo "Setting up meson build..."; \
	rm -rf $(SCX_C_BUILD_DIR); \
	$(MESON_SETUP_BASE) $(SCX_C_BUILD_DIR) $(MESON_SETUP_FLAGS) -Denable_rust=false || exit 1; \
	echo "Building all C schedulers..."; \
	meson compile -C $(SCX_C_BUILD_DIR) 2>/dev/null || meson compile -C $(SCX_C_BUILD_DIR) || exit 1; \
	echo "Copying C schedulers to sche_bin..."; \
	find $(SCX_C_BUILD_DIR) -name "scx_*" -type f -executable ! -name "*.o" ! -name "*.a" ! -name "*_test" ! -name "*.d" -exec cp {} ../sche_bin/ \; 2>/dev/null || true
	@echo "C schedulers built successfully!"

# Build Rust schedulers
build-rust: check-scx-submodule
	@echo "Building Rust SCX schedulers..."
	@mkdir -p sche_bin
	@cd scx && \
	echo "Setting up full meson build for Rust schedulers..."; \
	if [ ! -d $(SCX_BUILD_DIR) ]; then \
		$(MESON_SETUP_BASE) $(SCX_BUILD_DIR) $(MESON_SETUP_FLAGS) || exit 1; \
	fi; \
	echo "Building Rust schedulers via meson..."; \
	meson compile -C $(SCX_BUILD_DIR) || exit 1; \
	echo "Copying Rust schedulers from build to sche_bin..."; \
	find $(SCX_BUILD_DIR) -name "scx_*" -type f -executable ! -name "*.o" ! -name "*.a" ! -name "*_test" ! -name "*.d" -exec cp {} ../sche_bin/ \; 2>/dev/null || true; \
	echo "Also copying from target/release if available..."; \
	if [ -d target/release ]; then \
		for binary in target/release/scx_*; do \
			if [ -f "$$binary" ] && [ -x "$$binary" ] && ! echo "$$binary" | grep -E "\.d$$|\.rlib$$"; then \
				cp "$$binary" ../sche_bin/ 2>/dev/null && \
				echo "  ✓ $$(basename $$binary) copied to sche_bin"; \
			fi; \
		done; \
	fi
	@echo "Rust schedulers build complete!"

# Build tools (scx_loader, scxctl, scxtop)
build-tools: check-scx-submodule
	@echo "Building SCX tools..."
	@mkdir -p tools
	@cd scx && \
	echo "Building tools via meson..."; \
	if [ ! -d $(SCX_BUILD_DIR) ]; then \
		$(MESON_SETUP_BASE) $(SCX_BUILD_DIR) $(MESON_SETUP_FLAGS) || exit 1; \
	fi; \
	echo "Building scx_loader..."; \
	cargo build --release --manifest-path tools/scx_loader/Cargo.toml || exit 1; \
	echo "Building scxctl..."; \
	cargo build --release --manifest-path tools/scxctl/Cargo.toml || exit 1; \
	echo "Building scxtop..."; \
	cargo build --release --manifest-path tools/scxtop/Cargo.toml || exit 1; \
	echo "Copying tools to tools/ directory..."; \
	if [ -d target/release ]; then \
		for tool in scx_loader scxctl scxtop; do \
			if [ -f "target/release/$$tool" ]; then \
				cp "target/release/$$tool" ../tools/ && \
				echo "  ✓ $$tool copied to tools/"; \
			else \
				echo "  ✗ $$tool not found"; \
			fi; \
			done; \
		fi; \
	if [ -d $(SCX_BUILD_DIR)/debug ]; then \
		for tool in scx_loader scxctl scxtop; do \
			if [ ! -f "../tools/$$tool" ] && [ -f "$(SCX_BUILD_DIR)/debug/$$tool" ]; then \
				cp "$(SCX_BUILD_DIR)/debug/$$tool" ../tools/ && \
				echo "  ✓ $$tool copied to tools/"; \
			fi; \
		done; \
	fi; \
	echo "Looking for other tools in meson build..."; \
	if [ -d $(SCX_BUILD_DIR) ]; then \
		find $(SCX_BUILD_DIR) \( -name "dsq_lat" -o -name "runq_lat" -o -name "veristat*" \) -type f -executable | while read tool; do \
			cp "$$tool" ../tools/ 2>/dev/null && \
			echo "  ✓ $$(basename $$tool) copied to tools/"; \
		done; \
	fi
	@echo "Tools build complete!"
	@echo "Available tools:"
	@ls -la tools/ 2>/dev/null || echo "No tools found"

# Install dependencies
deps: install-deps

install-deps:
	@echo "Installing required dependencies for SCX build..."
	@echo "Detecting package manager..."
	@if command -v apt-get >/dev/null 2>&1; then \
		echo "Using apt-get (Debian/Ubuntu)..."; \
		sudo apt-get update && sudo apt-get install -y \
			build-essential \
			clang \
				llvm \
				libbpf-dev \
				libelf-dev \
				libz-dev \
			libzstd-dev \
			libseccomp-dev \
			pkg-config \
			cmake \
			ninja-build \
			python3-pip \
			python3-dev \
			meson \
			bpftool \
			jq \
			protobuf-compiler; \
		echo "Installing Rust if not present..."; \
		if ! command -v cargo >/dev/null 2>&1; then \
			curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; \
			source $$HOME/.cargo/env; \
		fi; \
	elif command -v dnf >/dev/null 2>&1; then \
		echo "Using dnf (Fedora/RHEL)..."; \
		sudo dnf install -y \
			clang \
				llvm \
				libbpf-devel \
				elfutils-libelf-devel \
				zlib-devel \
			libzstd-devel \
			libseccomp-devel \
			pkgconfig \
			cmake \
			ninja-build \
			python3-pip \
			python3-devel \
			meson \
			bpftool \
			jq \
			protobuf-compiler \
			rust \
			cargo; \
	elif command -v pacman >/dev/null 2>&1; then \
		echo "Using pacman (Arch Linux)..."; \
		sudo pacman -S --needed --noconfirm \
			base-devel \
			clang \
				llvm \
				libbpf \
				libelf \
				zlib \
			zstd \
			libseccomp \
			pkgconf \
			cmake \
			ninja \
			python-pip \
			meson \
			bpf \
			jq \
			protobuf \
			rust; \
	else \
		echo "ERROR: Unsupported package manager. Please install dependencies manually."; \
		echo "Required packages:"; \
		echo "  - Clang/LLVM >= 16 (17 recommended)"; \
		echo "  - Rust toolchain >= 1.82"; \
		echo "  - Meson >= 1.2.0"; \
		echo "  - libbpf >= 1.2.2 (1.3 recommended)"; \
		echo "  - libelf, libz, libzstd, libseccomp"; \
		echo "  - pkg-config, cmake, ninja-build"; \
		echo "  - bpftool, jq, protobuf-compiler"; \
		exit 1; \
	fi; \
	@echo "Checking kernel version..."; \
	@KERNEL_VERSION=$$(uname -r | cut -d. -f1-2 | sed 's/\.//'); \
	if [ $$KERNEL_VERSION -lt 612 ]; then \
		echo "WARNING: Kernel version $$(uname -r) detected."; \
		echo "sched-ext requires Linux kernel 6.12+ for proper operation."; \
	else \
		echo "Kernel version $$(uname -r) is compatible."; \
	fi; \
	@echo "Dependencies installation complete!"

# Clean build artifacts
clean:
	@echo "Cleaning SCX build artifacts..."
	@cd scx && rm -rf build target
	@cd scx/rust/scx_loader && rm -rf target 2>/dev/null || true
	@cd scx/tools/scxctl && rm -rf target 2>/dev/null || true
	@cd scx/tools/scxtop && rm -rf target 2>/dev/null || true
	@rm -rf sche_bin tools
	@echo "Clean complete!"

# Update submodule
update:
	@echo "Updating SCX submodule..."
	@git submodule update --init --recursive scx
	@cd scx && git pull origin main
	@echo "Update complete!"

# Generate documentation for all schedulers
doc:
	@echo "Generating scheduler documentation..."
	@mkdir -p sche_description
	@echo "Extracting C scheduler descriptions..."
	@cd scx && \
	for sched in simple central flatcg nest pair qmap userland prev sdt; do \
		echo "Processing scx_$$sched..."; \
		if [ -f "scheds/c/scx_$$sched.bpf.c" ]; then \
			echo "# scx_$$sched" > ../sche_description/scx_$$sched.md; \
			echo "" >> ../sche_description/scx_$$sched.md; \
			awk '/^\/\*/{p=1} p{print} /\*\//{if(p) exit}' scheds/c/scx_$$sched.bpf.c | \
			sed 's/^\/\*\s*//' | sed 's/^\s*\*\s*//' | sed 's/\s*\*\/$//' | \
			sed '/^$$/{N; /^\n$$/d}' >> ../sche_description/scx_$$sched.md; \
			grep -A50 "^## scx_$$sched" scheds/c/README.md 2>/dev/null | \
			awk '/^## scx_/{next} /^## scx_/{exit} {print}' >> ../sche_description/scx_$$sched.md 2>/dev/null || true; \
		fi; \
	done
	@echo "Copying Rust scheduler READMEs..."
	@cd scx/scheds/rust && \
	for sched_dir in scx_*/; do \
		sched=$$(basename $$sched_dir); \
		if [ -f "$$sched_dir/README.md" ] && [ -s "$$sched_dir/README.md" ]; then \
			cp "$$sched_dir/README.md" ../../../sche_description/$$sched.md; \
			echo "  ✓ Copied $$sched.md"; \
		else \
			echo "  ✗ No README for $$sched, creating from source..."; \
			echo "# $$sched" > ../../../sche_description/$$sched.md; \
			echo "" >> ../../../sche_description/$$sched.md; \
			if [ -f "$$sched_dir/src/bpf/main.bpf.c" ]; then \
				awk '/^\/\*/{p=1} p{print} /\*\//{if(p) exit}' "$$sched_dir/src/bpf/main.bpf.c" | \
				sed 's/^\/\*\s*//' | sed 's/^\s*\*\s*//' | sed 's/\s*\*\/$//' | \
				sed '/^$$/{N; /^\n$$/d}' >> ../../../sche_description/$$sched.md 2>/dev/null || \
				echo "No documentation found in source." >> ../../../sche_description/$$sched.md; \
			else \
				echo "No documentation found." >> ../../../sche_description/$$sched.md; \
			fi; \
		fi; \
	done
	@echo "Documentation generation complete!"
	@echo "Generated documentation files:"
	@ls -1 sche_description/*.md 2>/dev/null | wc -l
	@echo "files in sche_description/"

# Install schedulers to ~/.schedcp/scxbin/
install:
	@echo "Installing schedulers to ~/.schedcp/scxbin/..."
	@mkdir -p ~/.schedcp/scxbin
	@if [ -d sche_bin ]; then \
		echo "Copying schedulers from sche_bin/..."; \
		cp sche_bin/scx_* ~/.schedcp/scxbin/ 2>/dev/null || true; \
		echo "Setting executable permissions..."; \
		chmod +x ~/.schedcp/scxbin/scx_* 2>/dev/null || true; \
		echo "Installation complete!"; \
		echo "Installed schedulers:"; \
		ls -la ~/.schedcp/scxbin/ | grep -E "^-rwx" | wc -l; \
		echo "schedulers in ~/.schedcp/scxbin/"; \
	else \
		echo "ERROR: sche_bin directory not found."; \
		echo "Please run 'make build' first to build the schedulers."; \
		exit 1; \
	fi

# Help target
help:
	@echo "Available targets:"
	@echo "  make            - Build all schedulers and tools"
	@echo "  make deps       - Install required dependencies for building SCX"
	@echo "  make build      - Build all SCX schedulers (C and Rust) and copy to sche_bin/"
	@echo "  make build-c    - Build only C schedulers"
	@echo "  make build-rust - Build only Rust schedulers"
	@echo "  make build-tools- Build tools (scx_loader, scxctl, scxtop) and copy to tools/"
	@echo "  make doc        - Generate documentation for all schedulers in sche_description/"
	@echo "  make install    - Install schedulers to ~/.schedcp/scxbin/"
	@echo "  make clean      - Clean all build artifacts"
	@echo "  make update     - Update SCX submodule to latest version"
	@echo "  make help       - Show this help message"
