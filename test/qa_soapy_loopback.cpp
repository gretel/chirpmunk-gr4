#include <boost/ut.hpp>

#include <complex>
#include <cstring>
#include <vector>

#include <gnuradio-4.0/Scheduler.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>
#include <gnuradio-4.0/meta/formatter.hpp>
#include <gnuradio-4.0/soapysdr/SoapyDevSink.hpp>
#include <gnuradio-4.0/soapysdr/SoapyDevSource.hpp>
#include <gnuradio-4.0/testing/NullSources.hpp>

#if defined(__clang__)
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-W#warnings"
#elif defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wcpp"
#endif
#include <SoapySDR/Device.h>
#include <SoapySDR/Device.hpp>
#include <SoapySDR/Errors.hpp>
#include <SoapySDR/Formats.h>
#include <SoapySDR/Modules.h>
#include <SoapySDR/Modules.hpp>
#if defined(__clang__)
#pragma clang diagnostic pop
#elif defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

#ifndef SOAPY_LOOPBACK_MODULE_PATH
#error "SOAPY_LOOPBACK_MODULE_PATH must be defined by CMake"
#endif

const boost::ut::suite<"SoapyLoopback raw API"> rawApiTests = [] {
    using namespace boost::ut;

    "load loopback module"_test = [] {
        std::string err = SoapySDR::loadModule(SOAPY_LOOPBACK_MODULE_PATH);
        expect(err.empty()) << std::format("loadModule failed: {}", err);
    };

    "enumerate loopback device"_test = [] {
        auto devices = SoapySDR::Device::enumerate(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(ge(devices.size(), 1UZ)) << "no loopback device found";
    };

    "TX to RX data integrity (CF32)"_test = [] {
        auto* dev = SoapySDR::Device::make(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(dev != nullptr) << "failed to open loopback device";
        if (!dev) {
            return;
        }

        dev->setSampleRate(SOAPY_SDR_TX, 0, 1e6);
        dev->setSampleRate(SOAPY_SDR_RX, 0, 1e6);

        auto* txStream = dev->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        auto* rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
        expect(txStream != nullptr);
        expect(rxStream != nullptr);

        dev->activateStream(txStream);
        dev->activateStream(rxStream);

        constexpr std::size_t            nSamples = 1024;
        std::vector<std::complex<float>> txBuf(nSamples);
        for (std::size_t i = 0; i < nSamples; ++i) {
            txBuf[i] = {static_cast<float>(i), static_cast<float>(nSamples - i)};
        }

        const void* txBuffs[1] = {txBuf.data()};
        int         txFlags    = 0;
        int         txRet      = dev->writeStream(txStream, txBuffs, nSamples, txFlags);
        expect(eq(txRet, static_cast<int>(nSamples))) << std::format("writeStream returned {}", txRet);

        std::vector<std::complex<float>> rxBuf(nSamples);
        void*                            rxBuffs[1] = {rxBuf.data()};
        int                              rxFlags    = 0;
        long long                        rxTimeNs   = 0;
        int                              rxRet      = dev->readStream(rxStream, rxBuffs, nSamples, rxFlags, rxTimeNs, 1'000'000);
        expect(eq(rxRet, static_cast<int>(nSamples))) << std::format("readStream returned {}", rxRet);

        expect(eq(std::memcmp(txBuf.data(), rxBuf.data(), nSamples * sizeof(std::complex<float>)), 0)) << "TX/RX data mismatch";

        dev->deactivateStream(txStream);
        dev->deactivateStream(rxStream);
        dev->closeStream(txStream);
        dev->closeStream(rxStream);
        SoapySDR::Device::unmake(dev);
    };

    "timestamp round-trip"_test = [] {
        auto* dev = SoapySDR::Device::make(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(dev != nullptr);
        if (!dev) {
            return;
        }

        dev->setSampleRate(SOAPY_SDR_TX, 0, 1e6);
        dev->setSampleRate(SOAPY_SDR_RX, 0, 1e6);

        auto* txStream = dev->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        auto* rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
        dev->activateStream(txStream);
        dev->activateStream(rxStream);

        constexpr std::size_t            nSamples = 256;
        std::vector<std::complex<float>> txBuf(nSamples, {1.f, 0.f});
        const void*                      txBuffs[1] = {txBuf.data()};
        int                              txFlags    = 0;
        dev->writeStream(txStream, txBuffs, nSamples, txFlags);

        std::vector<std::complex<float>> rxBuf(nSamples);
        void*                            rxBuffs[1] = {rxBuf.data()};
        int                              rxFlags    = 0;
        long long                        rxTimeNs   = 0;
        int                              rxRet      = dev->readStream(rxStream, rxBuffs, nSamples, rxFlags, rxTimeNs, 1'000'000);
        expect(gt(rxRet, 0));
        expect((rxFlags & SOAPY_SDR_HAS_TIME) != 0) << "RX should have SOAPY_SDR_HAS_TIME flag";

        dev->deactivateStream(txStream);
        dev->deactivateStream(rxStream);
        dev->closeStream(txStream);
        dev->closeStream(rxStream);
        SoapySDR::Device::unmake(dev);
    };

    "overflow on full ring buffer"_test = [] {
        auto* dev = SoapySDR::Device::make(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(dev != nullptr);
        if (!dev) {
            return;
        }

        dev->setSampleRate(SOAPY_SDR_TX, 0, 1e6);
        dev->setSampleRate(SOAPY_SDR_RX, 0, 1e6);

        auto* txStream = dev->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        auto* rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
        dev->activateStream(txStream);
        dev->activateStream(rxStream);

        constexpr std::size_t            nSamples = 1024;
        std::vector<std::complex<float>> txBuf(nSamples, {0.f, 0.f});
        const void*                      txBuffs[1] = {txBuf.data()};

        bool gotOverflow = false;
        for (int i = 0; i < 20; ++i) {
            int flags = 0;
            int ret   = dev->writeStream(txStream, txBuffs, nSamples, flags);
            if (ret == SOAPY_SDR_OVERFLOW) {
                gotOverflow = true;
                break;
            }
        }
        expect(gotOverflow) << "expected overflow after filling ring buffer";

        dev->deactivateStream(txStream);
        dev->deactivateStream(rxStream);
        dev->closeStream(txStream);
        dev->closeStream(rxStream);
        SoapySDR::Device::unmake(dev);
    };

    "multiple write-read cycles"_test = [] {
        auto* dev = SoapySDR::Device::make(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(dev != nullptr);
        if (!dev) {
            return;
        }

        dev->setSampleRate(SOAPY_SDR_TX, 0, 1e6);
        dev->setSampleRate(SOAPY_SDR_RX, 0, 1e6);

        auto* txStream = dev->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        auto* rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
        dev->activateStream(txStream);
        dev->activateStream(rxStream);

        constexpr std::size_t nSamples = 512;
        constexpr int         nCycles  = 5;

        for (int cycle = 0; cycle < nCycles; ++cycle) {
            std::vector<std::complex<float>> txBuf(nSamples);
            for (std::size_t i = 0; i < nSamples; ++i) {
                auto val = static_cast<float>(static_cast<std::size_t>(cycle) * nSamples + i);
                txBuf[i] = {val, -val};
            }

            const void* txBuffs[1] = {txBuf.data()};
            int         txFlags    = 0;
            int         txRet      = dev->writeStream(txStream, txBuffs, nSamples, txFlags);
            expect(eq(txRet, static_cast<int>(nSamples))) << std::format("cycle {} writeStream returned {}", cycle, txRet);

            std::vector<std::complex<float>> rxBuf(nSamples);
            void*                            rxBuffs[1] = {rxBuf.data()};
            int                              rxFlags    = 0;
            long long                        rxTimeNs   = 0;
            int                              rxRet      = dev->readStream(rxStream, rxBuffs, nSamples, rxFlags, rxTimeNs, 1'000'000);
            expect(eq(rxRet, static_cast<int>(nSamples))) << std::format("cycle {} readStream returned {}", cycle, rxRet);
            expect(eq(std::memcmp(txBuf.data(), rxBuf.data(), nSamples * sizeof(std::complex<float>)), 0)) << std::format("cycle {} data mismatch", cycle);
        }

        dev->deactivateStream(txStream);
        dev->deactivateStream(rxStream);
        dev->closeStream(txStream);
        dev->closeStream(rxStream);
        SoapySDR::Device::unmake(dev);
    };

    "LoRa TX IQ round-trip"_test = [] {
        // Generate a LoRa frame: SF8, CR4, BW125k, OS=1, 8 preamble symbols
        const std::vector<uint8_t> payload = {'H', 'e', 'l', 'l', 'o'};
        auto                       iq      = gr::lora::generate_frame_iq(payload,
            /*sf=*/8, /*cr=*/4, /*os_factor=*/1,
            /*sync_word=*/0x12, /*preamble_len=*/8,
            /*has_crc=*/true, /*zero_pad=*/0,
            /*ldro_mode=*/0, /*bandwidth=*/125000,
            /*inverted_iq=*/false);
        expect(gt(iq.size(), 0UZ)) << "generate_frame_iq produced no samples";

        auto* dev = SoapySDR::Device::make(SoapySDR::Kwargs{{"driver", "loopback"}});
        expect(dev != nullptr);
        if (!dev) {
            return;
        }

        dev->setSampleRate(SOAPY_SDR_TX, 0, 125e3);
        dev->setSampleRate(SOAPY_SDR_RX, 0, 125e3);

        auto* txStream = dev->setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32);
        auto* rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
        expect(txStream != nullptr);
        expect(rxStream != nullptr);

        dev->activateStream(txStream);
        dev->activateStream(rxStream);

        // Write IQ data, possibly in chunks if larger than MTU
        const std::size_t mtu     = dev->getStreamMTU(txStream);
        std::size_t       written = 0;
        while (written < iq.size()) {
            std::size_t chunk      = std::min(mtu, iq.size() - written);
            const void* txBuffs[1] = {iq.data() + written};
            int         txFlags    = 0;
            int         txRet      = dev->writeStream(txStream, txBuffs, chunk, txFlags);
            expect(eq(txRet, static_cast<int>(chunk))) << std::format("writeStream returned {} at offset {}", txRet, written);
            if (txRet <= 0) {
                break;
            }
            written += static_cast<std::size_t>(txRet);
        }
        expect(eq(written, iq.size())) << "did not write all IQ samples";

        // Read back all IQ data (may arrive in multiple chunks)
        std::vector<std::complex<float>> rxBuf(iq.size());
        std::size_t                      totalRead = 0;
        while (totalRead < iq.size()) {
            std::size_t remaining  = iq.size() - totalRead;
            void*       rxBuffs[1] = {rxBuf.data() + totalRead};
            int         rxFlags    = 0;
            long long   rxTimeNs   = 0;
            int         rxRet      = dev->readStream(rxStream, rxBuffs, remaining, rxFlags, rxTimeNs, 1'000'000);
            expect(gt(rxRet, 0)) << std::format("readStream returned {} at offset {}", rxRet, totalRead);
            if (rxRet <= 0) {
                break;
            }
            totalRead += static_cast<std::size_t>(rxRet);
        }
        expect(eq(totalRead, iq.size())) << "did not read all IQ samples";

        // Verify bit-exact match
        expect(eq(std::memcmp(iq.data(), rxBuf.data(), iq.size() * sizeof(std::complex<float>)), 0)) << std::format("LoRa IQ mismatch ({} cf32 samples)", iq.size());

        dev->deactivateStream(txStream);
        dev->deactivateStream(rxStream);
        dev->closeStream(txStream);
        dev->closeStream(rxStream);
        SoapySDR::Device::unmake(dev);
    };

    "unload loopback module"_test = [] {
        std::string err = SoapySDR::unloadModule(SOAPY_LOOPBACK_MODULE_PATH);
        expect(err.empty()) << std::format("unloadModule failed: {}", err);
    };
};

const boost::ut::suite<"SoapyLoopback GR4 graph"> gr4GraphTests = [] {
    using namespace boost::ut;

    using TDuration = std::chrono::duration<std::chrono::steady_clock::rep, std::chrono::steady_clock::period>;
    using namespace std::chrono_literals;
    auto createWatchdog = [](auto& sched, TDuration timeOut = 5s, TDuration pollingPeriod = 40ms) {
        auto        externalInterventionNeeded = std::make_shared<std::atomic_bool>(false);
        std::thread watchdogThread([&sched, &externalInterventionNeeded, timeOut, pollingPeriod]() {
            auto timeout = std::chrono::steady_clock::now() + timeOut;
            while (std::chrono::steady_clock::now() < timeout) {
                if (sched.state() == gr::lifecycle::State::STOPPED) {
                    return;
                }
                std::this_thread::sleep_for(pollingPeriod);
            }
            std::println(stderr, "watchdog kicked in");
            externalInterventionNeeded->store(true, std::memory_order_relaxed);
            sched.requestStop();
            std::println(stderr, "requested scheduler to stop");
        });
        return std::make_pair(std::move(watchdogThread), externalInterventionNeeded);
    };

    "SoapySink TX via loopback"_test = [&createWatchdog] {
        std::string loadErr = SoapySDR::loadModule(SOAPY_LOOPBACK_MODULE_PATH);
        expect(loadErr.empty()) << std::format("loadModule failed: {}", loadErr);

        using namespace gr;
        using namespace gr::incubator::soapysdr;
        using namespace gr::testing;
        using scheduler = gr::scheduler::Simple<>;
        using ValueType = std::complex<float>;

        constexpr gr::Size_t nSamples = 10'000;

        gr::Graph flow;
        auto&     source = flow.emplaceBlock<ConstantSource<ValueType>>({{"n_samples_max", nSamples}});
        auto&     sink   = flow.emplaceBlock<SoapyDevSink<ValueType, 1UZ>>({
            {"device", "loopback"},
            {"sample_rate", float(1e6)},
            {"frequency", std::vector<double>{434e6}},
            {"timed_tx", false},
            {"wait_burst_ack", false},
        });
        expect(flow.connect<"out", "in">(source, sink).has_value());

        scheduler sched;
        if (auto ret = sched.exchange(std::move(flow)); !ret) {
            expect(false) << std::format("scheduler init failed: {}", ret.error());
            return;
        }
        auto [watchdogThread, externalInterventionNeeded] = createWatchdog(sched, 10s);

        auto retVal = sched.runAndWait();
        expect(retVal.has_value()) << std::format("scheduler error: {}", retVal.error());

        if (watchdogThread.joinable()) {
            watchdogThread.join();
        }
        expect(!externalInterventionNeeded->load(std::memory_order_relaxed)) << "watchdog kicked in — scheduler hung";

        std::println("'SoapySink TX via loopback' test finished");
        SoapySDR::unloadModule(SOAPY_LOOPBACK_MODULE_PATH);
    };

    // Note: a full-duplex graph test (ConstantSource → SoapySink + SoapyBlock → CountingSink)
    // is not possible because SoapySDR::Device::make() creates independent device instances —
    // the loopback ring buffer is not shared between separate make() calls.
    // The raw API suite above validates TX→RX data integrity directly.

    "SoapyBlock RX graph construction"_test = [] {
        std::string loadErr = SoapySDR::loadModule(SOAPY_LOOPBACK_MODULE_PATH);
        expect(loadErr.empty()) << std::format("loadModule failed: {}", loadErr);

        using namespace gr;
        using namespace gr::incubator::soapysdr;
        using namespace gr::testing;
        using ValueType = std::complex<float>;

        // verify SoapySource can be emplaced into a graph with the loopback driver
        gr::Graph flow;
        auto&     source = flow.emplaceBlock<SoapyDevSource<ValueType, 1UZ>>({
            {"device", "loopback"},
            {"sample_rate", float(1e6)},
            {"frequency", std::vector<double>{107e6}},
            {"verbose_overflow", false},
        });
        auto&     sink   = flow.emplaceBlock<CountingSink<ValueType>>({{"n_samples_max", gr::Size_t(1000)}});
        expect(flow.connect<"out", "in">(source, sink).has_value());

        std::println("'SoapyBlock RX graph construction' test finished");
        SoapySDR::unloadModule(SOAPY_LOOPBACK_MODULE_PATH);
    };
};

int main() { /* not needed for UT */ }
