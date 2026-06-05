#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/write_batch.h>

#include "schedcp_slug_marker.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <random>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Config {
    std::string db_path = "/tmp/rocksdb_tpcc";
    std::string query = "all";
    int warehouses = 4;
    int districts = 8;
    int customers = 1000;
    int items = 10000;
    int transactions = 20000;
    int threads = 4;
    int order_lines = 5;
    int value_size = 128;
};

struct QuerySpec {
    const char *name;
    double read_ops_per_tx;
    double write_ops_per_tx;
};

struct QueryResult {
    std::string name;
    uint64_t transactions = 0;
    double seconds = 0.0;
    double tps = 0.0;
    double avg_micros = 0.0;
    double read_ops_per_tx = 0.0;
    double write_ops_per_tx = 0.0;
    uint64_t errors = 0;
};

std::string arg_value(const char *arg, const char *name) {
    const size_t n = std::strlen(name);
    if (std::strncmp(arg, name, n) == 0 && arg[n] == '=') {
        return std::string(arg + n + 1);
    }
    return "";
}

int arg_int(const std::string &v, int fallback) {
    if (v.empty()) {
        return fallback;
    }
    return std::atoi(v.c_str());
}

Config parse_args(int argc, char **argv) {
    Config cfg;
    for (int i = 1; i < argc; ++i) {
        std::string v;
        if (!(v = arg_value(argv[i], "--db")).empty()) cfg.db_path = v;
        else if (!(v = arg_value(argv[i], "--query")).empty()) cfg.query = v;
        else if (!(v = arg_value(argv[i], "--warehouses")).empty()) cfg.warehouses = arg_int(v, cfg.warehouses);
        else if (!(v = arg_value(argv[i], "--districts")).empty()) cfg.districts = arg_int(v, cfg.districts);
        else if (!(v = arg_value(argv[i], "--customers")).empty()) cfg.customers = arg_int(v, cfg.customers);
        else if (!(v = arg_value(argv[i], "--items")).empty()) cfg.items = arg_int(v, cfg.items);
        else if (!(v = arg_value(argv[i], "--transactions")).empty()) cfg.transactions = arg_int(v, cfg.transactions);
        else if (!(v = arg_value(argv[i], "--threads")).empty()) cfg.threads = arg_int(v, cfg.threads);
        else if (!(v = arg_value(argv[i], "--order-lines")).empty()) cfg.order_lines = arg_int(v, cfg.order_lines);
        else if (!(v = arg_value(argv[i], "--value-size")).empty()) cfg.value_size = arg_int(v, cfg.value_size);
    }

    cfg.warehouses = std::max(1, cfg.warehouses);
    cfg.districts = std::max(1, cfg.districts);
    cfg.customers = std::max(1, cfg.customers);
    cfg.items = std::max(1, cfg.items);
    cfg.transactions = std::max(1, cfg.transactions);
    cfg.threads = std::max(1, cfg.threads);
    cfg.order_lines = std::max(1, cfg.order_lines);
    cfg.value_size = std::max(16, cfg.value_size);
    return cfg;
}

std::string value_for(const char *prefix, int id, int size) {
    std::string v = std::string(prefix) + ":" + std::to_string(id) + ":";
    if (static_cast<int>(v.size()) < size) {
        v.append(static_cast<size_t>(size - v.size()), 'x');
    }
    return v;
}

std::string warehouse_key(int w) {
    return "W:" + std::to_string(w);
}

std::string district_key(int w, int d) {
    return "D:" + std::to_string(w) + ":" + std::to_string(d);
}

std::string customer_key(int w, int d, int c) {
    return "C:" + std::to_string(w) + ":" + std::to_string(d) + ":" + std::to_string(c);
}

std::string item_key(int i) {
    return "I:" + std::to_string(i);
}

std::string stock_key(int w, int i) {
    return "S:" + std::to_string(w) + ":" + std::to_string(i);
}

std::string order_key(int w, int d, int c, uint64_t o) {
    return "O:" + std::to_string(w) + ":" + std::to_string(d) + ":" +
           std::to_string(c) + ":" + std::to_string(o);
}

std::string order_line_key(int w, int d, uint64_t o, int line) {
    return "L:" + std::to_string(w) + ":" + std::to_string(d) + ":" +
           std::to_string(o) + ":" + std::to_string(line);
}

std::string new_order_key(int w, int d, uint64_t o) {
    return "NO:" + std::to_string(w) + ":" + std::to_string(d) + ":" +
           std::to_string(o);
}

int uniform(std::mt19937_64 &rng, int low, int high) {
    std::uniform_int_distribution<int> dist(low, high);
    return dist(rng);
}

rocksdb::Status mark_get(rocksdb::DB *db, const rocksdb::ReadOptions &ro,
                         const std::string &key, std::string *value) {
    SLUG_MARK_READ_BB();
    return db->Get(ro, key, value);
}

rocksdb::Status mark_put(rocksdb::DB *db, const rocksdb::WriteOptions &wo,
                         const std::string &key, const std::string &value) {
    SLUG_MARK_WRITE_BB();
    return db->Put(wo, key, value);
}

rocksdb::Status mark_write(rocksdb::DB *db, const rocksdb::WriteOptions &wo,
                           rocksdb::WriteBatch *batch) {
    SLUG_MARK_WRITE_BB();
    return db->Write(wo, batch);
}

bool prepare_database(rocksdb::DB *db, const Config &cfg) {
    rocksdb::WriteOptions wo;
    wo.disableWAL = true;
    rocksdb::WriteBatch batch;
    int pending = 0;

    auto flush = [&]() {
        if (pending == 0) {
            return true;
        }
        auto st = mark_write(db, wo, &batch);
        batch.Clear();
        pending = 0;
        if (!st.ok()) {
            std::cerr << "prepare write failed: " << st.ToString() << "\n";
            return false;
        }
        return true;
    };

    for (int w = 1; w <= cfg.warehouses; ++w) {
        batch.Put(warehouse_key(w), value_for("warehouse", w, cfg.value_size));
        ++pending;
        for (int d = 1; d <= cfg.districts; ++d) {
            batch.Put(district_key(w, d), value_for("district", d, cfg.value_size));
            ++pending;
            for (int c = 1; c <= cfg.customers; ++c) {
                batch.Put(customer_key(w, d, c), value_for("customer", c, cfg.value_size));
                if (++pending >= 10000 && !flush()) {
                    return false;
                }
            }
        }
    }

    for (int i = 1; i <= cfg.items; ++i) {
        batch.Put(item_key(i), value_for("item", i, cfg.value_size));
        ++pending;
        for (int w = 1; w <= cfg.warehouses; ++w) {
            batch.Put(stock_key(w, i), value_for("stock", i, cfg.value_size));
            if (++pending >= 10000 && !flush()) {
                return false;
            }
        }
    }

    return flush();
}

bool do_new_order(rocksdb::DB *db, const Config &cfg, std::mt19937_64 &rng,
                  uint64_t seq) {
    rocksdb::ReadOptions ro;
    rocksdb::WriteOptions wo;
    wo.disableWAL = true;
    std::string value;
    const int w = uniform(rng, 1, cfg.warehouses);
    const int d = uniform(rng, 1, cfg.districts);
    const int c = uniform(rng, 1, cfg.customers);

    mark_get(db, ro, warehouse_key(w), &value);
    mark_get(db, ro, district_key(w, d), &value);
    mark_get(db, ro, customer_key(w, d, c), &value);

    rocksdb::WriteBatch batch;
    const uint64_t order_id = seq + 1;
    batch.Put(order_key(w, d, c, order_id), value_for("order", static_cast<int>(order_id), cfg.value_size));
    batch.Put(new_order_key(w, d, order_id), value_for("new_order", static_cast<int>(order_id), cfg.value_size));

    for (int line = 1; line <= cfg.order_lines; ++line) {
        const int item = uniform(rng, 1, cfg.items);
        mark_get(db, ro, item_key(item), &value);
        mark_get(db, ro, stock_key(w, item), &value);
        batch.Put(stock_key(w, item), value_for("stock_u", item + line, cfg.value_size));
        batch.Put(order_line_key(w, d, order_id, line), value_for("line", line, cfg.value_size));
    }

    return mark_write(db, wo, &batch).ok();
}

bool do_payment(rocksdb::DB *db, const Config &cfg, std::mt19937_64 &rng,
                uint64_t seq) {
    rocksdb::ReadOptions ro;
    rocksdb::WriteOptions wo;
    wo.disableWAL = true;
    std::string value;
    const int w = uniform(rng, 1, cfg.warehouses);
    const int d = uniform(rng, 1, cfg.districts);
    const int c = uniform(rng, 1, cfg.customers);

    mark_get(db, ro, warehouse_key(w), &value);
    mark_get(db, ro, district_key(w, d), &value);
    mark_get(db, ro, customer_key(w, d, c), &value);

    rocksdb::WriteBatch batch;
    batch.Put(warehouse_key(w), value_for("warehouse_paid", w, cfg.value_size));
    batch.Put(district_key(w, d), value_for("district_paid", d, cfg.value_size));
    batch.Put(customer_key(w, d, c), value_for("customer_paid", static_cast<int>(seq), cfg.value_size));
    return mark_write(db, wo, &batch).ok();
}

bool do_order_status(rocksdb::DB *db, const Config &cfg, std::mt19937_64 &rng,
                     uint64_t seq) {
    rocksdb::ReadOptions ro;
    std::string value;
    const int w = uniform(rng, 1, cfg.warehouses);
    const int d = uniform(rng, 1, cfg.districts);
    const int c = uniform(rng, 1, cfg.customers);
    const uint64_t order_id = seq > 0 ? seq : 1;

    mark_get(db, ro, customer_key(w, d, c), &value);
    mark_get(db, ro, order_key(w, d, c, order_id), &value);
    for (int line = 1; line <= cfg.order_lines; ++line) {
        mark_get(db, ro, order_line_key(w, d, order_id, line), &value);
    }
    return true;
}

bool do_delivery(rocksdb::DB *db, const Config &cfg, std::mt19937_64 &rng,
                 uint64_t seq) {
    rocksdb::ReadOptions ro;
    rocksdb::WriteOptions wo;
    wo.disableWAL = true;
    std::string value;
    const int w = uniform(rng, 1, cfg.warehouses);
    const int d = uniform(rng, 1, cfg.districts);
    const int c = uniform(rng, 1, cfg.customers);
    const uint64_t order_id = seq + 1;

    mark_get(db, ro, new_order_key(w, d, order_id), &value);
    mark_get(db, ro, order_key(w, d, c, order_id), &value);
    mark_get(db, ro, customer_key(w, d, c), &value);

    rocksdb::WriteBatch batch;
    batch.Delete(new_order_key(w, d, order_id));
    batch.Put(order_key(w, d, c, order_id), value_for("delivered", static_cast<int>(order_id), cfg.value_size));
    batch.Put(customer_key(w, d, c), value_for("customer_delivered", c, cfg.value_size));
    return mark_write(db, wo, &batch).ok();
}

bool do_stock_level(rocksdb::DB *db, const Config &cfg, std::mt19937_64 &rng,
                    uint64_t seq) {
    rocksdb::ReadOptions ro;
    std::string value;
    const int w = uniform(rng, 1, cfg.warehouses);
    const int d = uniform(rng, 1, cfg.districts);
    mark_get(db, ro, district_key(w, d), &value);

    const int start = uniform(rng, 1, std::max(1, cfg.items - 32));
    for (int i = 0; i < 32; ++i) {
        mark_get(db, ro, stock_key(w, start + i), &value);
    }
    (void)seq;
    return true;
}

QueryResult run_query(rocksdb::DB *db, const Config &cfg, const QuerySpec &spec,
                      uint64_t base_seq) {
    std::atomic<uint64_t> errors{0};
    std::vector<std::thread> threads;
    const int nthreads = std::max(1, cfg.threads);
    const uint64_t total = static_cast<uint64_t>(cfg.transactions);
    const uint64_t per_thread = total / static_cast<uint64_t>(nthreads);
    const uint64_t rem = total % static_cast<uint64_t>(nthreads);

    auto start = std::chrono::steady_clock::now();
    for (int tid = 0; tid < nthreads; ++tid) {
        const uint64_t count = per_thread + (static_cast<uint64_t>(tid) < rem ? 1 : 0);
        threads.emplace_back([&, tid, count]() {
            std::mt19937_64 rng(0xC0FFEEULL + base_seq + static_cast<uint64_t>(tid) * 9973ULL);
            for (uint64_t i = 0; i < count; ++i) {
                const uint64_t seq = base_seq + static_cast<uint64_t>(tid) * per_thread + i;
                bool ok = true;
                if (std::strcmp(spec.name, "new_order") == 0) {
                    ok = do_new_order(db, cfg, rng, seq);
                } else if (std::strcmp(spec.name, "payment") == 0) {
                    ok = do_payment(db, cfg, rng, seq);
                } else if (std::strcmp(spec.name, "order_status") == 0) {
                    ok = do_order_status(db, cfg, rng, seq);
                } else if (std::strcmp(spec.name, "delivery") == 0) {
                    ok = do_delivery(db, cfg, rng, seq);
                } else if (std::strcmp(spec.name, "stock_level") == 0) {
                    ok = do_stock_level(db, cfg, rng, seq);
                }
                if (!ok) {
                    errors.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }

    for (auto &t : threads) {
        t.join();
    }
    auto end = std::chrono::steady_clock::now();

    const double seconds = std::chrono::duration<double>(end - start).count();
    QueryResult result;
    result.name = spec.name;
    result.transactions = total;
    result.seconds = seconds;
    result.tps = seconds > 0.0 ? static_cast<double>(total) / seconds : 0.0;
    result.avg_micros = total > 0 ? seconds * 1e6 / static_cast<double>(total) : 0.0;
    result.read_ops_per_tx = spec.read_ops_per_tx;
    result.write_ops_per_tx = spec.write_ops_per_tx;
    result.errors = errors.load(std::memory_order_relaxed);
    return result;
}

void print_json(const Config &cfg, const std::vector<QueryResult> &results) {
    std::cout << "{\n";
    std::cout << "  \"config\": {\n";
    std::cout << "    \"db\": \"" << cfg.db_path << "\",\n";
    std::cout << "    \"query\": \"" << cfg.query << "\",\n";
    std::cout << "    \"warehouses\": " << cfg.warehouses << ",\n";
    std::cout << "    \"districts\": " << cfg.districts << ",\n";
    std::cout << "    \"customers\": " << cfg.customers << ",\n";
    std::cout << "    \"items\": " << cfg.items << ",\n";
    std::cout << "    \"transactions\": " << cfg.transactions << ",\n";
    std::cout << "    \"threads\": " << cfg.threads << ",\n";
    std::cout << "    \"order_lines\": " << cfg.order_lines << ",\n";
    std::cout << "    \"value_size\": " << cfg.value_size << "\n";
    std::cout << "  },\n";
    std::cout << "  \"results\": [\n";
    for (size_t i = 0; i < results.size(); ++i) {
        const auto &r = results[i];
        std::cout << "    {\"query\": \"" << r.name << "\", "
                  << "\"transactions\": " << r.transactions << ", "
                  << "\"seconds\": " << r.seconds << ", "
                  << "\"tps\": " << r.tps << ", "
                  << "\"avg_micros\": " << r.avg_micros << ", "
                  << "\"read_ops_per_tx\": " << r.read_ops_per_tx << ", "
                  << "\"write_ops_per_tx\": " << r.write_ops_per_tx << ", "
                  << "\"errors\": " << r.errors << "}";
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

    rocksdb::Options options;
    options.create_if_missing = true;
    options.compression = rocksdb::kNoCompression;
    options.IncreaseParallelism(cfg.threads);
    options.write_buffer_size = 128ULL << 20;
    options.max_write_buffer_number = 4;
    options.target_file_size_base = 64ULL << 20;

    std::unique_ptr<rocksdb::DB> db;
    auto st = rocksdb::DB::Open(options, cfg.db_path, &db);
    if (!st.ok()) {
        std::cerr << "open failed: " << st.ToString() << "\n";
        return 1;
    }

    if (!prepare_database(db.get(), cfg)) {
        return 2;
    }

    const QuerySpec all_specs[] = {
        {"new_order", 3.0 + cfg.order_lines * 2.0, 2.0 + cfg.order_lines * 2.0},
        {"payment", 3.0, 3.0},
        {"order_status", 2.0 + cfg.order_lines, 0.0},
        {"delivery", 3.0, 3.0},
        {"stock_level", 33.0, 0.0},
    };

    std::vector<QueryResult> results;
    uint64_t base_seq = 0;
    for (const auto &spec : all_specs) {
        if (cfg.query != "all" && cfg.query != spec.name) {
            continue;
        }
        results.push_back(run_query(db.get(), cfg, spec, base_seq));
        base_seq += static_cast<uint64_t>(cfg.transactions) + 1000000ULL;
    }

    if (results.empty()) {
        std::cerr << "unknown query: " << cfg.query << "\n";
        return 3;
    }

    print_json(cfg, results);
    return 0;
}
