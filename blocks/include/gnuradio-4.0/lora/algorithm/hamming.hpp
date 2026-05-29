// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_HAMMING_HPP
#define GNURADIO_LORA_HAMMING_HPP

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <span>
#include <vector>

#include <gnuradio-4.0/lora/algorithm/utilities.hpp>

namespace gr::lora {

/// Hamming encode a 4-bit nibble with the given coding rate (cr_app = 1..4).
/// Returns the encoded codeword as a (4+cr_app)-bit value stored right-aligned.
///
/// Bit layout (Hamming(7,4) systematic encoding):
///   For cr_app >= 2: out = [d3 d2 d1 d0 | p0 p1 p2 p3] >> (4 - cr_app)
///   For cr_app == 1: out = [d3 d2 d1 d0 | p4]
[[nodiscard]] inline uint8_t hamming_encode(uint8_t nibble, uint8_t cr_app) {
    auto d = int2bool(nibble, 4); // MSB-first: d[0]=MSB, d[3]=LSB
    // Data bits are reversed (d[3]=MSB of codeword) per LoRa convention.
    // data_bin[3] (LSB of nibble) goes to MSB of output codeword.

    if (cr_app != 1) {
        bool p0 = d[3] ^ d[2] ^ d[1];
        bool p1 = d[2] ^ d[1] ^ d[0];
        bool p2 = d[3] ^ d[2] ^ d[0];
        bool p3 = d[3] ^ d[1] ^ d[0];
        // Output: data_bin[3]<<7 | data_bin[2]<<6 | data_bin[1]<<5 | data_bin[0]<<4 | p0<<3 | ...
        uint8_t full = static_cast<uint8_t>((d[3] << 7) | (d[2] << 6) | (d[1] << 5) | (d[0] << 4) | (p0 << 3) | (p1 << 2) | (p2 << 1) | p3);
        return static_cast<uint8_t>(full >> (4 - cr_app));
    } else {
        // CR = 4/5: single parity bit
        bool p4 = d[0] ^ d[1] ^ d[2] ^ d[3];
        return static_cast<uint8_t>((d[3] << 4) | (d[2] << 3) | (d[1] << 2) | (d[0] << 1) | p4);
    }
}

/// Hamming-encode a full frame of nibbles.
/// The first (sf - 2) nibbles always use cr_app=4 regardless of the nominal CR.
[[nodiscard]] inline std::vector<uint8_t> hamming_encode_frame(std::span<const uint8_t> nibbles, uint8_t sf, uint8_t cr) {
    std::vector<uint8_t> encoded;
    encoded.reserve(nibbles.size());
    for (std::size_t i = 0; i < nibbles.size(); i++) {
        uint8_t cr_app = (static_cast<int>(i) < sf - 2) ? uint8_t(4) : cr;
        encoded.push_back(hamming_encode(nibbles[i], cr_app));
    }
    return encoded;
}

/// Hard Hamming decode a codeword (cr_app + 4 bits wide) → 4-bit nibble.
/// Hamming hard decoding: syndrome-based single-bit error correction.
[[nodiscard]] inline uint8_t hamming_decode_hard(uint8_t codeword, uint8_t cr_app) {
    auto cw = int2bool(codeword, cr_app + 4);
    // Data bits: reorganised MSB-first: data_nibble = {cw[3], cw[2], cw[1], cw[0]}
    std::vector<bool> data_nibble = {cw[3], cw[2], cw[1], cw[0]};

    switch (cr_app) {
    case 4: {
        // Don't correct if even number of 1s (double error → uncorrectable)
        int ones = 0;
        for (bool b : cw) {
            ones += b;
        }
        if (ones % 2 == 0) {
            break;
        }
    }
        [[fallthrough]];
    case 3: {
        bool s0      = cw[0] ^ cw[1] ^ cw[2] ^ cw[4];
        bool s1      = cw[1] ^ cw[2] ^ cw[3] ^ cw[5];
        bool s2      = cw[0] ^ cw[1] ^ cw[3] ^ cw[6];
        int  syndrom = s0 + (s1 << 1) + (s2 << 2);
        switch (syndrom) {
        case 5: data_nibble[3].flip(); break;
        case 7: data_nibble[2].flip(); break;
        case 3: data_nibble[1].flip(); break;
        case 6: data_nibble[0].flip(); break;
        default: break; // parity bit error or no error
        }
        break;
    }
    case 2: {
        // Detect-only (no correction)
        break;
    }
    case 1: {
        // Parity-only: detect single error but can't correct
        break;
    }
    }

    return static_cast<uint8_t>(bool2int(data_nibble));
}

/// Hamming LUT for soft ML decoding (cr >= 4/6).
/// Each entry encodes [d0 d1 d2 d3 | p0 p1 p2 p3] = 8-bit codeword.
inline constexpr std::array<uint8_t, 16> hamming_cw_LUT = {0, 23, 45, 58, 78, 89, 99, 116, 139, 156, 166, 177, 197, 210, 232, 255};

/// Hamming LUT for soft ML decoding with CR = 4/5.
inline constexpr std::array<uint8_t, 16> hamming_cw_LUT_cr5 = {0, 24, 40, 48, 72, 80, 96, 120, 136, 144, 160, 184, 192, 216, 232, 240};

/// Soft Hamming decode using ML-LUT: find the codeword with maximum likelihood.
/// codeword_LLR has cw_len elements (cr_app + 4), positive LLR → bit 1 more likely.
/// Returns 4-bit data nibble.
[[nodiscard]] inline uint8_t hamming_decode_soft(const LLR* codeword_LLR, uint8_t cr_app) {
    const uint8_t cw_len = cr_app + 4;
    constexpr int cw_nbr = 16;

    std::array<LLR, cw_nbr> cw_proba{};

    for (std::size_t n = 0; n < cw_nbr; n++) {
        for (std::size_t j = 0; j < cw_len; j++) {
            uint8_t lut_entry = (cr_app != 1) ? hamming_cw_LUT[n] : hamming_cw_LUT_cr5[n];
            bool    bit       = (lut_entry >> (8 - cw_len)) & (1u << (cw_len - 1 - j));
            if ((bit && codeword_LLR[j] > 0) || (!bit && codeword_LLR[j] < 0)) {
                cw_proba[n] += std::abs(codeword_LLR[j]);
            } else {
                cw_proba[n] -= std::abs(codeword_LLR[j]);
            }
        }
    }

    // Select codeword with maximum probability
    auto    it        = std::max_element(cw_proba.begin(), cw_proba.end());
    auto    idx_max   = static_cast<std::size_t>(it - cw_proba.begin());
    uint8_t data_soft = hamming_cw_LUT[idx_max] >> 4;

    // Reverse bit order (MSB<=>LSB within 4 bits) per LoRa convention
    return static_cast<uint8_t>(((data_soft & 0b0001) << 3) | ((data_soft & 0b0010) << 1) // bit1 -> bit2
                                | ((data_soft & 0b0100) >> 1)                             // bit2 -> bit1
                                | ((data_soft & 0b1000) >> 3));
}

} // namespace gr::lora

#endif // GNURADIO_LORA_HAMMING_HPP
