// SPDX-License-Identifier: ISC
//
// generate_vectors — produce LoRa TX pipeline test vectors for all SF×CR×BW configs.
//
// Usage: generate_vectors [test_vectors_dir]
//   Defaults to ../test_vectors relative to the executable.
//
// Writes one subdirectory per config: sf{SF}_cr{CR}_bw{BW}/
//   config.json          — parameters as JSON
//   payload.txt          — raw payload string
//   tx_01_whitened_nibbles.u8
//   tx_02_with_header.u8
//   tx_03_with_crc.u8
//   tx_04_encoded.u8
//   tx_05_interleaved.u32
//   tx_06_gray_mapped.u32
//   tx_07_iq_frame.cf32

#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>

#include <complex>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using namespace gr::lora;

// --- Config matrix -----------------------------------------------------------

struct Config {
    uint8_t  sf;
    uint8_t  cr;
    uint32_t bw;
};

static constexpr uint8_t  SF_VALUES[] = {7, 8, 9, 10, 11, 12};
static constexpr uint8_t  CR_VALUES[] = {1, 2, 3, 4};
static constexpr uint32_t BW_VALUES[] = {62500, 125000};

static constexpr uint16_t SYNC_WORD     = 0x12;
static constexpr uint16_t PREAMBLE_LEN  = 8;
static constexpr uint8_t  OS_FACTOR     = 4;
static constexpr bool     HAS_CRC       = true;
static constexpr uint8_t  ZERO_PAD_SYMS = 5; // trailing silence (matches GR3 test vectors)

// Payload long enough to exercise multiple interleaver blocks but short enough
// to keep file sizes reasonable.  14 bytes matches the existing GR3 vectors.
static const std::string PAYLOAD_STR = "Hello MeshCore";

// --- File writers ------------------------------------------------------------

static void write_u8(const fs::path& path, const std::vector<uint8_t>& data) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size()));
}

static void write_u32(const fs::path& path, const std::vector<uint32_t>& data) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size() * sizeof(uint32_t)));
}

static void write_cf32(const fs::path& path, const std::vector<std::complex<float>>& data) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size() * sizeof(std::complex<float>)));
}

static void write_text(const fs::path& path, const std::string& text) {
    std::ofstream f(path);
    f << text;
}

static void write_config_json(const fs::path& path, const Config& cfg) {
    std::ofstream f(path);
    f << "{\n"
      << "  \"sf\": " << static_cast<int>(cfg.sf) << ",\n"
      << "  \"bw\": " << cfg.bw << ",\n"
      << "  \"cr\": " << static_cast<int>(cfg.cr) << ",\n"
      << "  \"sync_word\": " << SYNC_WORD << ",\n"
      << "  \"impl_head\": false,\n"
      << "  \"has_crc\": " << (HAS_CRC ? "true" : "false") << ",\n"
      << "  \"preamble_len\": " << PREAMBLE_LEN << ",\n"
      << "  \"os_factor\": " << static_cast<int>(OS_FACTOR) << ",\n"
      << "  \"payload\": \"" << PAYLOAD_STR << "\"\n"
      << "}\n";
}

// --- Generate one config's vectors -------------------------------------------

static void generate_config(const fs::path& base_dir, const Config& cfg) {
    const std::string dir_name = "sf" + std::to_string(cfg.sf) + "_cr" + std::to_string(cfg.cr) + "_bw" + std::to_string(cfg.bw);

    const fs::path dir = base_dir / dir_name;
    fs::create_directories(dir);

    const std::vector<uint8_t> payload(PAYLOAD_STR.begin(), PAYLOAD_STR.end());
    const uint8_t              payload_len = static_cast<uint8_t>(payload.size());
    const bool                 use_ldro    = needs_ldro(cfg.sf, cfg.bw);

    // Stage 1: whiten
    auto whitened = whiten(payload);
    write_u8(dir / "tx_01_whitened_nibbles.u8", whitened);

    // Stage 2: insert header
    auto with_header = insert_header(whitened, payload_len, cfg.cr, HAS_CRC);
    write_u8(dir / "tx_02_with_header.u8", with_header);

    // Stage 3: add CRC
    auto with_crc = add_crc(with_header, payload, HAS_CRC);
    write_u8(dir / "tx_03_with_crc.u8", with_crc);

    // Stage 4: Hamming encode
    auto encoded = hamming_encode_frame(with_crc, cfg.sf, cfg.cr);
    write_u8(dir / "tx_04_encoded.u8", encoded);

    // Stage 5: interleave
    auto interleaved = interleave_frame(encoded, cfg.sf, cfg.cr, use_ldro);
    write_u32(dir / "tx_05_interleaved.u32", interleaved);

    // Stage 6: gray demap
    auto gray_mapped = gray_demap(interleaved, cfg.sf);
    write_u32(dir / "tx_06_gray_mapped.u32", gray_mapped);

    // Stage 7: modulate to IQ
    const uint32_t sps      = (1u << cfg.sf) * OS_FACTOR;
    const uint32_t zero_pad = sps * ZERO_PAD_SYMS;
    auto           iq       = modulate_frame(gray_mapped, cfg.sf, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, zero_pad, false);
    write_cf32(dir / "tx_07_iq_frame.cf32", iq);

    // Metadata
    write_text(dir / "payload.txt", PAYLOAD_STR);
    write_config_json(dir / "config.json", cfg);

    std::fprintf(stderr, "  %s: %zu nibbles, %zu symbols, %zu IQ samples%s\n", dir_name.c_str(), with_crc.size(), gray_mapped.size(), iq.size(), use_ldro ? " (LDRO)" : "");
}

// --- Main --------------------------------------------------------------------

int main(int argc, char* argv[]) {
    fs::path base_dir;
    if (argc > 1) {
        base_dir = argv[1];
    } else {
        // Default: ../test_vectors relative to executable
        base_dir = fs::path(argv[0]).parent_path().parent_path() / "test_vectors";
    }

    std::fprintf(stderr, "Generating test vectors in: %s\n", base_dir.c_str());

    int count = 0;
    for (auto sf : SF_VALUES) {
        for (auto cr : CR_VALUES) {
            for (auto bw : BW_VALUES) {
                generate_config(base_dir, {sf, cr, bw});
                ++count;
            }
        }
    }

    std::fprintf(stderr, "Done: %d configs generated.\n", count);
    return 0;
}
