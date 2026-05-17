// SPDX-License-Identifier: ISC
#pragma once

#include <complex>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <boost/ut.hpp>

namespace gr::lora::test {

// --- Test vector config ------------------------------------------------------

struct VectorConfig {
    uint8_t     sf           = 8;
    uint32_t    bw           = 62500;
    uint8_t     cr           = 4;
    uint16_t    sync_word    = 0x12;
    bool        has_crc      = true;
    uint16_t    preamble_len = 8;
    uint8_t     os_factor    = 4;
    std::string payload      = "Hello MeshCore";

    [[nodiscard]] uint32_t N() const { return 1u << sf; }
    [[nodiscard]] uint32_t SPS() const { return N() * os_factor; }

    [[nodiscard]] std::string subdir() const { return "sf" + std::to_string(sf) + "_cr" + std::to_string(cr) + "_bw" + std::to_string(bw); }
};

inline const VectorConfig DEFAULT_CONFIG{};

// Legacy constants for backward compatibility
inline constexpr uint8_t  SF           = 8;
inline constexpr uint32_t N            = 1u << SF;
inline constexpr uint8_t  CR           = 4;
inline constexpr uint32_t BW           = 62500;
inline constexpr uint8_t  OS_FACTOR    = 4;
inline constexpr uint16_t SYNC_WORD    = 0x12;
inline constexpr uint16_t PREAMBLE_LEN = 8;
inline constexpr uint32_t CENTER_FREQ  = 866'000'000;
inline constexpr bool     HAS_CRC      = true;
inline constexpr uint32_t SPS          = N * OS_FACTOR;

// --- Full config matrix ------------------------------------------------------

inline std::vector<VectorConfig> allConfigs() {
    std::vector<VectorConfig> configs;
    for (auto sf : {uint8_t(7), uint8_t(8), uint8_t(9), uint8_t(10), uint8_t(11), uint8_t(12)}) {
        for (auto cr : {uint8_t(1), uint8_t(2), uint8_t(3), uint8_t(4)}) {
            for (uint32_t bw : {62500u, 125000u}) {
                configs.push_back({sf, bw, cr});
            }
        }
    }
    return configs;
}

// --- File loaders ------------------------------------------------------------

inline const std::filesystem::path& testVectorDir() {
    static const std::filesystem::path dir = TEST_VECTORS_DIR;
    return dir;
}

inline std::vector<uint8_t> load_u8(const VectorConfig& cfg, const std::string& filename) {
    auto          path = testVectorDir() / cfg.subdir() / filename;
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("Cannot open test vector: " + path.string());
    }
    return {std::istreambuf_iterator<char>(f), {}};
}

inline std::vector<uint32_t> load_u32(const VectorConfig& cfg, const std::string& filename) {
    auto          path = testVectorDir() / cfg.subdir() / filename;
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("Cannot open test vector: " + path.string());
    }
    std::vector<uint32_t> data;
    uint32_t              val;
    while (f.read(reinterpret_cast<char*>(&val), sizeof(val))) {
        data.push_back(val);
    }
    return data;
}

inline std::vector<std::complex<float>> load_cf32(const VectorConfig& cfg, const std::string& filename) {
    auto          path = testVectorDir() / cfg.subdir() / filename;
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("Cannot open test vector: " + path.string());
    }
    std::vector<std::complex<float>> data;
    float                            re, im;
    while (f.read(reinterpret_cast<char*>(&re), sizeof(re)) && f.read(reinterpret_cast<char*>(&im), sizeof(im))) {
        data.emplace_back(re, im);
    }
    return data;
}

inline std::string load_text(const VectorConfig& cfg, const std::string& filename) {
    auto          path = testVectorDir() / cfg.subdir() / filename;
    std::ifstream f(path);
    if (!f) {
        throw std::runtime_error("Cannot open test vector: " + path.string());
    }
    return {std::istreambuf_iterator<char>(f), {}};
}

// Legacy loaders — default config
inline std::vector<uint8_t>             load_u8(const std::string& filename) { return load_u8(DEFAULT_CONFIG, filename); }
inline std::vector<uint32_t>            load_u32(const std::string& filename) { return load_u32(DEFAULT_CONFIG, filename); }
inline std::vector<std::complex<float>> load_cf32(const std::string& filename) { return load_cf32(DEFAULT_CONFIG, filename); }
inline std::string                      load_text(const std::string& filename) { return load_text(DEFAULT_CONFIG, filename); }

// --- Test comparators --------------------------------------------------------

template<typename T>
void expect_vectors_equal(const std::vector<T>& actual, const std::vector<T>& expected, const std::string& label = "") {
    using namespace boost::ut;
    expect(actual.size() == expected.size()) << label << " size mismatch: " << actual.size() << " vs " << expected.size();
    for (size_t i = 0; i < std::min(actual.size(), expected.size()); ++i) {
        expect(actual[i] == expected[i]) << label << " mismatch at index " << i << ": got " << actual[i] << ", expected " << expected[i];
    }
}

} // namespace gr::lora::test
