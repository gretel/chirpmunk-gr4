// SPDX-License-Identifier: ISC
//
// iio_smoke — minimal RX bring-up against an AD9361 over libiio (FISH Ball /
// Adalm-Pluto / tezuka_fw). Confirms gr4-lora can consume the gr4-incubator
// IIO blocks. No tagging, no CBOR, no decoder — just samples in and a count.
// For full-pipeline RX use lora_scan (later: with --backend=iio).

#include <atomic>
#include <chrono>
#include <complex>
#include <cstddef>
#include <format>
#include <iostream>
#include <string>
#include <thread>

#include <gnuradio-4.0/Graph.hpp>
#include <gnuradio-4.0/Scheduler.hpp>
#include <gnuradio-4.0/iio/IIOSource.hpp>
#include <gnuradio-4.0/testing/NullSources.hpp>

#include <CLI/CLI.hpp>

int main(int argc, char** argv) {
    CLI::App app{"gr4-lora IIO smoke: open libiio context, capture N samples"};

    std::string uri              = "ip:10.0.23.149";
    std::string device           = "cf-ad9361-lpc";
    std::string phy_device       = "ad9361-phy";
    float       sample_rate      = 2'083'334.0F;
    double      center_frequency = 868'100'000.0;
    double      bandwidth        = 200'000.0;
    double      gain             = 40.0;
    std::string gain_mode        = "slow_attack";
    std::size_t buffer_size      = 32'768;
    std::size_t timeout_ms       = 1'000;
    std::size_t n_samples        = 200'000;
    double      timeout_s        = 10.0;

    app.add_option("--uri", uri, "libiio URI (default: ip:10.0.23.149)");
    app.add_option("--device", device, "Streaming device");
    app.add_option("--phy", phy_device, "Tuning device");
    app.add_option("--rate", sample_rate, "Sample rate (Hz, AD9361 floor ~2.083 MHz)");
    app.add_option("--freq", center_frequency, "Center frequency (Hz)");
    app.add_option("--bw", bandwidth, "Bandwidth (Hz)");
    app.add_option("--gain", gain, "Gain (dB)");
    app.add_option("--gain-mode", gain_mode, "manual|slow_attack|fast_attack");
    app.add_option("--buffer", buffer_size, "DMA buffer size (samples)");
    app.add_option("--timeout-ms", timeout_ms, "libiio context timeout (ms)");
    app.add_option("-n,--samples", n_samples, "Sample count before stopping");
    app.add_option("--timeout", timeout_s, "Wall-clock watchdog (s, 0 = disable)");

    CLI11_PARSE(app, argc, argv);

    using T = std::complex<float>;

    gr::Graph fg;

    auto& source = fg.emplaceBlock<gr::incubator::iio::IIOSource<T>>({
        {"uri", uri},
        {"device", device},
        {"phy_device", phy_device},
        {"sample_rate", sample_rate},
        {"center_frequency", center_frequency},
        {"bandwidth", bandwidth},
        {"gain", gain},
        {"gain_mode", gain_mode},
        {"buffer_size", static_cast<gr::Size_t>(buffer_size)},
        {"timeout_ms", static_cast<gr::Size_t>(timeout_ms)},
    });

    auto& sink = fg.emplaceBlock<gr::testing::CountingSink<T>>({
        {"n_samples_max", static_cast<gr::Size_t>(n_samples)},
    });

    if (auto conn = fg.connect(source, gr::PortDefinition{"out"}, sink, gr::PortDefinition{"in"}); !conn) {
        throw gr::exception(std::format("connect failed: {}", conn.error().message));
    }

    gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreaded> sched;
    if (auto ret = sched.exchange(std::move(fg)); !ret) {
        throw std::runtime_error(std::format("scheduler init failed: {}", ret.error()));
    }

    std::atomic<bool> done{false};
    std::thread       watchdog;
    if (timeout_s > 0.0) {
        watchdog = std::thread([&] {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_s);
            while (!done.load(std::memory_order_relaxed) && std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            if (!done.load(std::memory_order_relaxed)) {
                sink.requestStop();
            }
        });
    }

    const auto ret = sched.runAndWait();
    done.store(true, std::memory_order_relaxed);
    if (watchdog.joinable()) {
        watchdog.join();
    }
    if (!ret.has_value()) {
        std::cerr << "scheduler error: " << ret.error().message << "\n";
        return 1;
    }

    std::cout << "Captured samples: " << sink.count << "\n";
    return 0;
}
