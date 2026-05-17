// SPDX-License-Identifier: ISC
/// Smoke tests for IIO graph builder paths (integrated into build_rx_graph /
/// build_scan_graph via cfg.device == "iio").
///
/// Verifies IIOSource property_map keys match expectations from the IIO
/// config file without requiring actual Pluto hardware. No scheduler is
/// started — graph construction and property reflection only.
///
/// Gated by GR4_LORA_HAS_IIO (the test target is only created when libiio
/// and the gr4-incubator IIO headers are available).

#include <string>
#include <string_view>
#include <vector>

#include <boost/ut.hpp>

#include <gnuradio-4.0/Graph.hpp>
#include <gnuradio-4.0/ValueHelper.hpp> // pmt::ValueVisitor

#include "../apps/config.hpp"
#include "../apps/graph_builder.hpp"

using namespace boost::ut;

#ifndef CONFIG_TOML_PATH
#error "CONFIG_TOML_PATH must be defined by CMake"
#endif

namespace {

/// Extract a string value from a pmt::Value (property_map entry).
std::string extractStr(const gr::pmt::Value& val) {
    std::string result;
    gr::pmt::ValueVisitor([&](const auto& v) {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, std::string_view>) {
            result = std::string(v);
        }
    }).visit(val);
    return result;
}

/// Extract a double value from a pmt::Value.
double extractDouble(const gr::pmt::Value& val) {
    double result = -1.0;
    gr::pmt::ValueVisitor([&](const auto& v) {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, double>) {
            result = v;
        }
    }).visit(val);
    return result;
}

/// Extract a float value from a pmt::Value.
float extractFloat(const gr::pmt::Value& val) {
    float result = -1.0f;
    gr::pmt::ValueVisitor([&](const auto& v) {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, float>) {
            result = v;
        } else if constexpr (std::is_same_v<T, double>) {
            result = static_cast<float>(v);
        }
    }).visit(val);
    return result;
}

/// Extract a bool value from a pmt::Value.
bool extractBool(const gr::pmt::Value& val) {
    bool result = false;
    gr::pmt::ValueVisitor([&](const auto& v) {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, bool>) {
            result = v;
        }
    }).visit(val);
    return result;
}

/// Extract an unsigned integer size value from a pmt::Value.
gr::Size_t extractSize(const gr::pmt::Value& val) {
    gr::Size_t result = 0;
    gr::pmt::ValueVisitor([&](const auto& v) {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, gr::Size_t>) {
            result = v;
        } else if constexpr (std::is_same_v<T, uint32_t>) {
            result = static_cast<gr::Size_t>(v);
        }
    }).visit(val);
    return result;
}

/// Load the first [trx] config from the IIO config file and verify
/// the IIO config fields are present.
lora_config::TrxConfig loadTrxForIIO() {
    auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
    expect(fatal(cfgs.size() >= 1_u)) << "load_config returned empty";
    auto c = cfgs[0];
    expect(c.device == "iio") << "config should have driver=\"iio\"";
    expect(c.rx_channels.size() >= 1_u) << "config should have at least one rx_channel";
    // URI is now embedded in device_param as "uri=..."
    expect(!c.device_param.empty()) << "config should have device_param set";
    return c;
}

/// Load the [scan] config from the IIO config file.
lora_config::ScanSetConfig loadScanForIIO() {
    auto cfgs = lora_config::load_scan_config(CONFIG_TOML_PATH);
    expect(fatal(cfgs.size() >= 1_u)) << "load_scan_config returned empty";
    auto c = cfgs[0];
    expect(c.device == "iio") << "scan config should have driver=\"iio\"";
    return c;
}

} // namespace

const boost::ut::suite iioGraphBuilderSmoke = [] {
    "build_rx_graph sets expected IIOSource property keys for iio device"_test = [] {
        auto      cfg = loadTrxForIIO();
        gr::Graph graph;

        std::atomic<gr::Size_t>* overflow_ptr = nullptr;
        expect(nothrow([&] {
            overflow_ptr = lora_graph::build_rx_graph(graph, cfg,
                [](const std::vector<uint8_t>&, bool, uint16_t) {},
                nullptr, nullptr, {});
        })) << "build_rx_graph (iio) should not throw";

        // IIO source uses its own private overflow counter; build_rx_graph
        // returns nullptr instead of a SoapySource-style _overflowCount ptr.
        // This is expected — overflow monitoring goes through find_iio_source
        // + read_source_overflow instead.
        expect(overflow_ptr == nullptr) << "IIO overflow_ptr should be nullptr";

        // Inspect blocks post-construction (pre-exchange)
        auto blocks   = graph.blocks();
        auto iio_src  = lora_graph::find_iio_source(blocks);
        expect(iio_src != nullptr) << "IIOSource should be found via find_iio_source";

        auto props = iio_src->settings().get();

        // Verify all expected keys exist with correct values
        auto checkStr = [&](std::string_view key, std::string_view expected) {
            auto it = props.find(std::pmr::string(key));
            expect(it != props.end()) << key << " key present";
            if (it != props.end()) {
                auto val = extractStr(it->second);
                expect(val == expected) << key << " == " << expected << " (got: " << val << ")";
            }
        };
        auto checkDouble = [&](std::string_view key, double expected) {
            auto it = props.find(std::pmr::string(key));
            expect(it != props.end()) << key << " key present";
            if (it != props.end()) {
                auto val = extractDouble(it->second);
                expect(val == expected) << key << " == " << expected << " (got: " << val << ")";
            }
        };
        auto checkFloat = [&](std::string_view key, float expected) {
            auto it = props.find(std::pmr::string(key));
            expect(it != props.end()) << key << " key present";
            if (it != props.end()) {
                auto val = extractFloat(it->second);
                expect(val == expected) << key << " == " << expected;
            }
        };
        auto checkBool = [&](std::string_view key, bool expected) {
            auto it = props.find(std::pmr::string(key));
            expect(it != props.end()) << key << " key present";
            if (it != props.end()) {
                auto val = extractBool(it->second);
                expect(val == expected) << key << " == " << (expected ? "true" : "false");
            }
        };

        // URI is extracted from device_param (stripping "uri=" prefix)
        std::string uri = cfg.device_param;
        if (uri.starts_with("uri=")) {
            uri = uri.substr(4);
        }
        checkStr("uri", uri);
        checkFloat("sample_rate", cfg.rate);
        checkDouble("center_frequency", cfg.freq);
        checkDouble("gain", cfg.gain_rx);
        checkStr("gain_mode", "slow_attack");

        // rf_port: when rx_channel[0] == 1 → "B_BALANCED", else "A_BALANCED"
        std::string expected_rf = (cfg.rx_channels[0] == 1U) ? "B_BALANCED" : "A_BALANCED";
        checkStr("rf_port", expected_rf);

        // buffer_size = 32768
        {
            auto it = props.find(std::pmr::string("buffer_size"));
            expect(it != props.end()) << "buffer_size key present";
            if (it != props.end()) {
                auto val = extractSize(it->second);
                expect(val == 32768_u) << "buffer_size == 32768 (got: " << val << ")";
            }
        }

        checkBool("non_blocking", false);

        // max_overflow_count should be present
        {
            auto it = props.find(std::pmr::string("max_overflow_count"));
            expect(it != props.end()) << "max_overflow_count key present";
        }
    };

    "find_source_block returns IIOSource when only IIO present"_test = [] {
        auto      cfg = loadTrxForIIO();
        gr::Graph graph;

        expect(nothrow([&] {
            lora_graph::build_rx_graph(graph, cfg,
                [](const std::vector<uint8_t>&, bool, uint16_t) {},
                nullptr, nullptr, {});
        }));

        auto blocks = graph.blocks();

        auto src = lora_graph::find_source_block(blocks);
        expect(src != nullptr) << "find_source_block should find IIOSource";

        auto iio_src = lora_graph::find_iio_source(blocks);
        expect(iio_src != nullptr) << "find_iio_source should find IIOSource";

        auto soapy_src = lora_graph::find_soapy_source(blocks);
        expect(soapy_src == nullptr) << "find_soapy_source should return nullptr for IIO-only graph";
    };

    "read_source_overflow works for IIOSource"_test = [] {
        auto      cfg = loadTrxForIIO();
        gr::Graph graph;

        expect(nothrow([&] {
            lora_graph::build_rx_graph(graph, cfg,
                [](const std::vector<uint8_t>&, bool, uint16_t) {},
                nullptr, nullptr, {});
        }));

        auto blocks = graph.blocks();
        auto src    = lora_graph::find_source_block(blocks);
        expect(src != nullptr);

        auto ovf = lora_graph::read_source_overflow(src);
        expect(ovf == 0_u) << "overflow count should be 0 on never-started block";
    };

    "build_scan_graph constructs IIOSource with scan defaults for iio device"_test = [] {
        auto      cfg = loadScanForIIO();
        gr::Graph graph;

        std::vector<double> channels{cfg.center_freq()};
        lora_graph::ScanGraph sg;

        expect(nothrow([&] {
            sg = lora_graph::build_scan_graph(graph, cfg, channels);
        })) << "build_scan_graph (iio) should not throw";

        auto blocks  = graph.blocks();
        auto iio_src = lora_graph::find_iio_source(blocks);
        expect(iio_src != nullptr) << "IIOSource found in scan graph";

        auto props = iio_src->settings().get();

        // Scan path uses "slow_attack" AGC
        {
            auto it = props.find(std::pmr::string("gain_mode"));
            expect(it != props.end()) << "gain_mode key present in scan";
            if (it != props.end()) {
                auto val = extractStr(it->second);
                expect(val == "slow_attack") << "scan gain_mode == slow_attack (got: " << val << ")";
            }
        }

        // SpectrumState and CaptureState should be created
        expect(sg.spectrum != nullptr) << "scan graph should have SpectrumState";
        expect(sg.capture != nullptr) << "scan graph should have CaptureState";
    };
};

int main() { /* boost::ut drives the suite */ }
