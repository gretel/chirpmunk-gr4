// SPDX-License-Identifier: ISC
//
// tx_chain.hpp — Complete LoRa TX chain as pure algorithm functions.
//
// This header provides the full LoRa transmitter pipeline from payload bytes
// to baseband IQ samples, without any GR4 graph or block dependencies. Each
// stage is exposed as a separate function for testability, and the top-level
// generate_frame_iq() ties them together.
//
// Pipeline:
//   payload bytes -> whiten -> insert_header -> add_crc -> hamming_encode
//   -> interleave -> gray_demap -> modulate -> IQ samples

#ifndef GNURADIO_LORA_TX_CHAIN_HPP
#define GNURADIO_LORA_TX_CHAIN_HPP

#include <complex>
#include <cstdint>
#include <span>
#include <vector>

#include <gnuradio-4.0/lora/algorithm/crc.hpp>
#include <gnuradio-4.0/lora/algorithm/hamming.hpp>
#include <gnuradio-4.0/lora/algorithm/interleaving.hpp>
#include <gnuradio-4.0/lora/algorithm/tables.hpp>
#include <gnuradio-4.0/lora/algorithm/utilities.hpp>

namespace gr::lora {

/// Whitening: each payload byte -> 2 nibbles XORed with LFSR sequence.
[[nodiscard]] inline std::vector<uint8_t> whiten(std::span<const uint8_t> payload) {
    std::vector<uint8_t> nibbles;
    nibbles.reserve(payload.size() * 2);
    for (std::size_t i = 0; i < payload.size(); i++) {
        uint8_t ws       = whitening_seq[i % whitening_seq.size()];
        uint8_t low_nib  = (payload[i] & 0x0F) ^ (ws & 0x0F);
        uint8_t high_nib = ((payload[i] >> 4) & 0x0F) ^ ((ws >> 4) & 0x0F);
        nibbles.push_back(low_nib);
        nibbles.push_back(high_nib);
    }
    return nibbles;
}

/// Dewhitening: reassemble nibble pairs into bytes, XOR payload bytes with LFSR.
/// Processes data_nibble_count/2 nibble pairs starting at nibbles[0].
/// The first pay_len bytes are dewhitened; remaining bytes (CRC) are not.
[[nodiscard]] inline std::vector<uint8_t> dewhiten(std::span<const uint8_t> nibbles, uint32_t pay_len) {
    std::vector<uint8_t> bytes;
    bytes.reserve(nibbles.size() / 2);
    for (std::size_t i = 0; i + 1 < nibbles.size(); i += 2) {
        uint8_t low_nib  = nibbles[i];
        uint8_t high_nib = nibbles[i + 1];
        auto    byte_idx = i / 2;
        if (byte_idx < pay_len) {
            uint8_t ws = whitening_seq[byte_idx % whitening_seq.size()];
            low_nib ^= (ws & 0x0F);
            high_nib ^= ((ws >> 4) & 0x0F);
        }
        bytes.push_back(static_cast<uint8_t>((high_nib << 4) | low_nib));
    }
    return bytes;
}

/// Insert explicit header (5 nibbles) before whitened payload nibbles.
[[nodiscard]] inline std::vector<uint8_t> insert_header(std::span<const uint8_t> whitened_nibbles, uint8_t payload_len, uint8_t cr, bool has_crc) {
    auto                 hdr = build_explicit_header(payload_len, cr, has_crc);
    std::vector<uint8_t> result;
    result.reserve(5 + whitened_nibbles.size());
    result.insert(result.end(), hdr.begin(), hdr.end());
    result.insert(result.end(), whitened_nibbles.begin(), whitened_nibbles.end());
    return result;
}

/// Append CRC-16/XMODEM as 4 whitened nibbles.
/// CRC nibbles are whitened with the same continuous LFSR sequence used
/// for payload bytes, making TX compatible with SX1262 receivers which
/// whiten the entire data stream including CRC.
[[nodiscard]] inline std::vector<uint8_t> add_crc(std::span<const uint8_t> with_header, std::span<const uint8_t> payload, bool has_crc) {
    std::vector<uint8_t> result(with_header.begin(), with_header.end());
    if (has_crc && payload.size() >= 2) {
        auto nibs = crc_to_nibbles(lora_payload_crc(payload));
        // Whitening sequence continues from payload: CRC bytes at index
        // pay_len and pay_len+1 use whitening_seq[pay_len..pay_len+1].
        for (std::size_t byte_idx = 0u; byte_idx < 2u; ++byte_idx) {
            const uint8_t ws = whitening_seq[(payload.size() + byte_idx) % whitening_seq.size()];
            nibs[byte_idx * 2u] ^= (ws & 0x0F);
            nibs[byte_idx * 2u + 1u] ^= ((ws >> 4u) & 0x0F);
        }
        result.insert(result.end(), nibs.begin(), nibs.end());
    }
    return result;
}

/// Gray de-mapping: binary -> Gray code + 1 cyclic offset.
[[nodiscard]] inline std::vector<uint32_t> gray_demap(std::span<const uint32_t> symbols, uint8_t sf) {
    const auto            n = static_cast<int64_t>(1u << sf);
    std::vector<uint32_t> result;
    result.reserve(symbols.size());
    for (auto sym : symbols) {
        uint32_t g = sym;
        for (int j = 1; j < sf; j++) {
            g ^= (sym >> j);
        }
        result.push_back(static_cast<uint32_t>(mod(static_cast<int64_t>(g) + 1, n)));
    }
    return result;
}

/// Modulate symbols into baseband IQ chirps with preamble, sync word,
/// and SFD.
///
/// When inverted_iq is true, the chirp roles are swapped: preamble uses
/// downchirps, SFD uses upchirps, and payload uses downchirps. This
/// produces a "downchirp frame" (inverted IQ) as used by some LoRa
/// devices and network configurations.
[[nodiscard]] inline std::vector<std::complex<float>> modulate_frame(std::span<const uint32_t> symbols, uint8_t sf, uint8_t os_factor, uint16_t sync_word, uint16_t preamble_len, uint32_t zero_pad = 0, bool inverted_iq = false) {
    const uint32_t                   sps = (1u << sf) * os_factor;
    std::vector<std::complex<float>> iq;

    std::vector<std::complex<float>> upchirp(sps), downchirp(sps);
    build_ref_chirps(upchirp.data(), downchirp.data(), sf, os_factor);

    // For inverted IQ: swap chirp roles. Preamble/payload use downchirps,
    // SFD uses upchirps. Equivalently, conjugate the entire output.
    // We implement by swapping the reference chirps used for each role.
    auto& preamble_chirp = inverted_iq ? downchirp : upchirp;
    auto& sfd_chirp      = inverted_iq ? upchirp : downchirp;

    auto sw0 = static_cast<uint16_t>(((sync_word & 0xF0) >> 4) << 3);
    auto sw1 = static_cast<uint16_t>((sync_word & 0x0F) << 3);

    // Preamble
    for (uint16_t i = 0; i < preamble_len; i++) {
        iq.insert(iq.end(), preamble_chirp.begin(), preamble_chirp.end());
    }

    // 2 sync word chirps (same direction as preamble, with symbol offset)
    {
        std::vector<std::complex<float>> sw_chirp(sps);
        build_upchirp(sw_chirp.data(), sw0, sf, os_factor);
        if (inverted_iq) {
            for (auto& s : sw_chirp) {
                s = std::conj(s);
            }
        }
        iq.insert(iq.end(), sw_chirp.begin(), sw_chirp.end());
    }
    {
        std::vector<std::complex<float>> sw_chirp(sps);
        build_upchirp(sw_chirp.data(), sw1, sf, os_factor);
        if (inverted_iq) {
            for (auto& s : sw_chirp) {
                s = std::conj(s);
            }
        }
        iq.insert(iq.end(), sw_chirp.begin(), sw_chirp.end());
    }

    // SFD: 2 full chirps + quarter chirp (opposite direction from preamble)
    iq.insert(iq.end(), sfd_chirp.begin(), sfd_chirp.end());
    iq.insert(iq.end(), sfd_chirp.begin(), sfd_chirp.end());
    iq.insert(iq.end(), sfd_chirp.begin(), sfd_chirp.begin() + static_cast<std::ptrdiff_t>(sps / 4));

    // Payload chirps (same direction as preamble)
    for (auto sym : symbols) {
        std::vector<std::complex<float>> chirp(sps);
        build_upchirp(chirp.data(), sym, sf, os_factor);
        if (inverted_iq) {
            for (auto& s : chirp) {
                s = std::conj(s);
            }
        }
        iq.insert(iq.end(), chirp.begin(), chirp.end());
    }

    // Zero padding
    if (zero_pad > 0) {
        iq.resize(iq.size() + zero_pad, std::complex<float>(0.f, 0.f));
    }

    return iq;
}

/// Determine whether LDRO (Low Data Rate Optimization) should be active
/// for the given SF and bandwidth.
[[nodiscard]] inline bool needs_ldro(uint8_t sf, uint32_t bandwidth) { return (static_cast<float>(1u << sf) * 1e3f / static_cast<float>(bandwidth)) > LDRO_MAX_DURATION_MS; }

/// Full TX chain: payload bytes -> baseband IQ samples.
///
/// Ties together the complete LoRa TX pipeline:
///   whiten -> insert_header -> add_crc -> hamming_encode -> interleave
///   -> gray_demap -> modulate
///
/// The ldro_mode parameter controls Low Data Rate Optimization:
///   0 = off, 1 = on, 2 = auto (enabled when symbol duration > 16ms).
/// Auto mode requires bandwidth to be set; when ldro_mode != 2, bandwidth
/// is unused.
///
/// When inverted_iq is true, the frame uses downchirp preamble (inverted
/// IQ). The RX side detects this by correlating with conjugated input.
[[nodiscard]] inline std::vector<std::complex<float>> generate_frame_iq(std::span<const uint8_t> payload, uint8_t sf, uint8_t cr, uint8_t os_factor, uint16_t sync_word, uint16_t preamble_len, bool has_crc = true, uint32_t zero_pad = 0, uint8_t ldro_mode = 2, uint32_t bandwidth = 62500, bool inverted_iq = false) {
    bool ldro = (ldro_mode == 2) ? needs_ldro(sf, bandwidth) : (ldro_mode != 0);

    auto whitened    = whiten(payload);
    auto with_header = insert_header(whitened, static_cast<uint8_t>(payload.size()), cr, has_crc);
    auto with_crc    = add_crc(with_header, payload, has_crc);
    auto encoded     = hamming_encode_frame(with_crc, sf, cr);
    auto interleaved = interleave_frame(encoded, sf, cr, ldro);
    auto gray_mapped = gray_demap(interleaved, sf);
    return modulate_frame(gray_mapped, sf, os_factor, sync_word, preamble_len, zero_pad, inverted_iq);
}

} // namespace gr::lora

#endif // GNURADIO_LORA_TX_CHAIN_HPP
