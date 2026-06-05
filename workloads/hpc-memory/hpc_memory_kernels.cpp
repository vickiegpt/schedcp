#include "schedcp_slug_marker.h"

#include <omp.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <numeric>
#include <random>
#include <string>
#include <vector>

namespace {

struct Config {
    std::string kernel = "all";
    std::string slug_hint = "pipeline";
    int threads = 0;
    int iterations = 3;
    size_t size_mib = 256;
};

struct Result {
    std::string kernel;
    double seconds = 0.0;
    double bandwidth_gb_s = 0.0;
    double checksum = 0.0;
    uint64_t bytes = 0;
};

std::string arg_value(const char *arg, const char *name) {
    const size_t n = std::strlen(name);
    if (std::strncmp(arg, name, n) == 0 && arg[n] == '=') {
        return std::string(arg + n + 1);
    }
    return "";
}

Config parse_args(int argc, char **argv) {
    Config cfg;
    for (int i = 1; i < argc; ++i) {
        std::string v;
        if (!(v = arg_value(argv[i], "--kernel")).empty()) cfg.kernel = v;
        else if (!(v = arg_value(argv[i], "--slug-hint")).empty()) cfg.slug_hint = v;
        else if (!(v = arg_value(argv[i], "--threads")).empty()) cfg.threads = std::atoi(v.c_str());
        else if (!(v = arg_value(argv[i], "--iterations")).empty()) cfg.iterations = std::atoi(v.c_str());
        else if (!(v = arg_value(argv[i], "--size-mib")).empty()) cfg.size_mib = static_cast<size_t>(std::strtoull(v.c_str(), nullptr, 10));
    }
    cfg.iterations = std::max(1, cfg.iterations);
    cfg.size_mib = std::max<size_t>(16, cfg.size_mib);
    return cfg;
}

void mark_hint(const std::string &hint) {
    if (hint == "read") {
        SLUG_MARK_READ_BB();
    } else if (hint == "write") {
        SLUG_MARK_WRITE_BB();
    } else if (hint == "balanced") {
        SLUG_MARK_BALANCED_BB();
    } else {
        SLUG_MARK_PIPELINE_BB();
    }
}

double now_sec() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

size_t elems_for_mib(size_t mib) {
    return (mib * 1024ULL * 1024ULL) / sizeof(double);
}

double checksum_stride(const std::vector<double> &v) {
    const size_t stride = std::max<size_t>(1, v.size() / 1024);
    double sum = 0.0;
    for (size_t i = 0; i < v.size(); i += stride) {
        sum += v[i];
    }
    return sum;
}

Result stream_triad(const Config &cfg) {
    const size_t n = elems_for_mib(cfg.size_mib);
    std::vector<double> a(n, 1.0), b(n, 2.0), c(n, 3.0), d(n, 4.0);
    const double scalar = 0.25;
    const double start = now_sec();

    for (int it = 0; it < cfg.iterations; ++it) {
#pragma omp parallel
        {
            mark_hint(cfg.slug_hint);
#pragma omp for schedule(static)
            for (size_t i = 0; i < n; ++i) {
                a[i] = b[i] + scalar * c[i] + d[i];
            }
        }
        std::swap(a, d);
    }

    const double seconds = now_sec() - start;
    const uint64_t bytes = static_cast<uint64_t>(cfg.iterations) * n * sizeof(double) * 4ULL;
    return {"stream_triad", seconds, bytes / seconds / 1e9, checksum_stride(a), bytes};
}

Result wrf_stencil(const Config &cfg) {
    const size_t target = elems_for_mib(cfg.size_mib);
    const size_t nx = std::max<size_t>(32, static_cast<size_t>(std::cbrt(static_cast<double>(target))));
    const size_t ny = nx;
    const size_t nz = std::max<size_t>(32, target / (nx * ny));
    const size_t n = nx * ny * nz;
    std::vector<double> in(n, 1.0), out(n, 0.0);
    const double start = now_sec();

    for (int it = 0; it < cfg.iterations; ++it) {
#pragma omp parallel
        {
            mark_hint(cfg.slug_hint);
#pragma omp for schedule(static)
            for (size_t z = 1; z < nz - 1; ++z) {
                for (size_t y = 1; y < ny - 1; ++y) {
                    const size_t base = (z * ny + y) * nx;
                    for (size_t x = 1; x < nx - 1; ++x) {
                        const size_t i = base + x;
                        out[i] = 0.18 * in[i] + 0.12 * (
                            in[i - 1] + in[i + 1] +
                            in[i - nx] + in[i + nx] +
                            in[i - nx * ny] + in[i + nx * ny]);
                    }
                }
            }
        }
        std::swap(in, out);
    }

    const double seconds = now_sec() - start;
    const uint64_t updates = static_cast<uint64_t>(cfg.iterations) *
        static_cast<uint64_t>((nx - 2) * (ny - 2) * (nz - 2));
    const uint64_t bytes = updates * sizeof(double) * 8ULL;
    return {"wrf_stencil", seconds, bytes / seconds / 1e9, checksum_stride(in), bytes};
}

Result gromacs_pairlist(const Config &cfg) {
    const size_t atoms = std::max<size_t>(1024, elems_for_mib(cfg.size_mib) / 8);
    const int neighbors = 12;
    std::vector<double> x(atoms), y(atoms), z(atoms), fx(atoms), fy(atoms), fz(atoms);
    std::vector<uint32_t> nbr(atoms * neighbors);

    for (size_t i = 0; i < atoms; ++i) {
        x[i] = 0.001 * static_cast<double>(i % 1024);
        y[i] = 0.002 * static_cast<double>((i * 3) % 1024);
        z[i] = 0.003 * static_cast<double>((i * 7) % 1024);
        for (int j = 0; j < neighbors; ++j) {
            nbr[i * neighbors + j] = static_cast<uint32_t>((i + 17ULL * j + 1) % atoms);
        }
    }

    const double start = now_sec();
    for (int it = 0; it < cfg.iterations; ++it) {
#pragma omp parallel
        {
            mark_hint(cfg.slug_hint);
#pragma omp for schedule(static)
            for (size_t i = 0; i < atoms; ++i) {
                double ax = 0.0, ay = 0.0, az = 0.0;
                const double xi = x[i], yi = y[i], zi = z[i];
                for (int j = 0; j < neighbors; ++j) {
                    const uint32_t k = nbr[i * neighbors + j];
                    const double dx = x[k] - xi;
                    const double dy = y[k] - yi;
                    const double dz = z[k] - zi;
                    const double inv = 1.0 / (0.01 + dx * dx + dy * dy + dz * dz);
                    ax += dx * inv;
                    ay += dy * inv;
                    az += dz * inv;
                }
                fx[i] = ax;
                fy[i] = ay;
                fz[i] = az;
            }
        }
    }

    const double seconds = now_sec() - start;
    const uint64_t bytes_per_atom = neighbors * (sizeof(uint32_t) + 3ULL * sizeof(double)) + 6ULL * sizeof(double);
    const uint64_t bytes = static_cast<uint64_t>(cfg.iterations) * atoms * bytes_per_atom;
    return {"gromacs_pairlist", seconds, bytes / seconds / 1e9, checksum_stride(fx) + checksum_stride(fy), bytes};
}

Result sst_sparse(const Config &cfg) {
    const size_t n = std::max<size_t>(1024, elems_for_mib(cfg.size_mib) / 4);
    const int degree = 16;
    std::vector<double> value(n, 1.0), next(n, 0.0), weight(n * degree, 0.0625);
    std::vector<uint32_t> index(n * degree);
    for (size_t i = 0; i < n; ++i) {
        for (int d = 0; d < degree; ++d) {
            index[i * degree + d] = static_cast<uint32_t>((i * 1315423911ULL + d * 2654435761ULL) % n);
        }
    }

    const double start = now_sec();
    for (int it = 0; it < cfg.iterations; ++it) {
#pragma omp parallel
        {
            mark_hint(cfg.slug_hint);
#pragma omp for schedule(static)
            for (size_t i = 0; i < n; ++i) {
                double sum = 0.0;
                for (int d = 0; d < degree; ++d) {
                    const size_t p = i * degree + d;
                    sum += value[index[p]] * weight[p];
                }
                next[i] = 0.5 * value[i] + sum;
            }
        }
        std::swap(value, next);
    }

    const double seconds = now_sec() - start;
    const uint64_t bytes_per_node = degree * (sizeof(uint32_t) + 2ULL * sizeof(double)) + 2ULL * sizeof(double);
    const uint64_t bytes = static_cast<uint64_t>(cfg.iterations) * n * bytes_per_node;
    return {"sst_sparse", seconds, bytes / seconds / 1e9, checksum_stride(value), bytes};
}

Result quantum_state(const Config &cfg) {
    size_t amps = elems_for_mib(cfg.size_mib) / 2;
    amps = std::max<size_t>(1024, amps);
    size_t pow2 = 1;
    while (pow2 * 2 <= amps) {
        pow2 *= 2;
    }
    amps = pow2;

    std::vector<double> real(amps, 0.0), imag(amps, 0.0);
    real[0] = 1.0;
    for (size_t i = 1; i < amps; ++i) {
        real[i] = 1.0 / std::sqrt(static_cast<double>(amps));
        imag[i] = 0.5 / std::sqrt(static_cast<double>(amps));
    }

    const double inv_sqrt2 = 1.0 / std::sqrt(2.0);
    const double start = now_sec();
    for (int it = 0; it < cfg.iterations; ++it) {
        const size_t bit = static_cast<size_t>(it % std::max(1, static_cast<int>(std::log2(amps))));
        const size_t step = 1ULL << bit;
        const size_t span = step << 1U;
#pragma omp parallel
        {
            mark_hint(cfg.slug_hint);
#pragma omp for schedule(static)
            for (size_t base = 0; base < amps; base += span) {
                for (size_t j = 0; j < step; ++j) {
                    const size_t i0 = base + j;
                    const size_t i1 = i0 + step;
                    const double r0 = real[i0], i0v = imag[i0];
                    const double r1 = real[i1], i1v = imag[i1];
                    real[i0] = (r0 + r1) * inv_sqrt2;
                    imag[i0] = (i0v + i1v) * inv_sqrt2;
                    real[i1] = (r0 - r1) * inv_sqrt2;
                    imag[i1] = (i0v - i1v) * inv_sqrt2;
                }
            }
        }
    }

    const double seconds = now_sec() - start;
    const uint64_t bytes = static_cast<uint64_t>(cfg.iterations) * amps * sizeof(double) * 4ULL;
    return {"quantum_state", seconds, bytes / seconds / 1e9, checksum_stride(real) + checksum_stride(imag), bytes};
}

bool want(const std::string &selected, const std::string &name) {
    if (selected == "all" || selected == name) {
        return true;
    }

    size_t start = 0;
    while (start <= selected.size()) {
        const size_t end = selected.find(',', start);
        std::string token = selected.substr(
            start,
            end == std::string::npos ? std::string::npos : end - start);
        token.erase(std::remove_if(token.begin(), token.end(), ::isspace), token.end());
        if (token == name || (token == "quantum_simulator" && name == "quantum_state")) {
            return true;
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }
    return false;
}

void print_json(const Config &cfg, const std::vector<Result> &results) {
    std::cout << "{\n";
    std::cout << "  \"config\": {\n";
    std::cout << "    \"kernel\": \"" << cfg.kernel << "\",\n";
    std::cout << "    \"slug_hint\": \"" << cfg.slug_hint << "\",\n";
    std::cout << "    \"threads\": " << (cfg.threads > 0 ? cfg.threads : omp_get_max_threads()) << ",\n";
    std::cout << "    \"iterations\": " << cfg.iterations << ",\n";
    std::cout << "    \"size_mib\": " << cfg.size_mib << "\n";
    std::cout << "  },\n";
    std::cout << "  \"results\": [\n";
    for (size_t i = 0; i < results.size(); ++i) {
        const auto &r = results[i];
        std::cout << "    {\"kernel\": \"" << r.kernel << "\", "
                  << "\"seconds\": " << r.seconds << ", "
                  << "\"bandwidth_gb_s\": " << r.bandwidth_gb_s << ", "
                  << "\"bytes\": " << r.bytes << ", "
                  << "\"checksum\": " << r.checksum << "}";
        if (i + 1 != results.size()) {
            std::cout << ",";
        }
        std::cout << "\n";
    }
    std::cout << "  ]\n";
    std::cout << "}\n";
}

} // namespace

int main(int argc, char **argv) {
    Config cfg = parse_args(argc, argv);
    if (cfg.threads > 0) {
        omp_set_num_threads(cfg.threads);
    }

    std::vector<Result> results;
    if (want(cfg.kernel, "stream_triad")) {
        results.push_back(stream_triad(cfg));
    }
    if (want(cfg.kernel, "wrf_stencil")) {
        results.push_back(wrf_stencil(cfg));
    }
    if (want(cfg.kernel, "gromacs_pairlist")) {
        results.push_back(gromacs_pairlist(cfg));
    }
    if (want(cfg.kernel, "sst_sparse")) {
        results.push_back(sst_sparse(cfg));
    }
    if (want(cfg.kernel, "quantum_state")) {
        results.push_back(quantum_state(cfg));
    }

    if (results.empty()) {
        std::cerr << "unknown kernel: " << cfg.kernel << "\n";
        return 1;
    }

    print_json(cfg, results);
    return 0;
}
