// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_CRC_HPP
#define GNURADIO_LORA_CRC_HPP

#include <array>
#include <cstdint>
#include <span>

namespace gr::lora {

/// CRC-16/CCITT: polynomial 0x1021, init 0x0000.
/// Processes one byte at a time, bit-by-bit (MSB first).
/// Standard CRC-16/XMODEM bit-by-bit implementation.
[[nodiscard]] inline uint16_t crc16_byte(uint16_t crc, uint8_t byte) {
    for (int i = 0; i < 8; i++) {
        if (((crc & 0x8000) >> 8) ^ (byte & 0x80)) {
            crc = static_cast<uint16_t>((crc << 1) ^ 0x1021);
        } else {
            crc = static_cast<uint16_t>(crc << 1);
        }
        byte <<= 1;
    }
    return crc;
}

/// Compute CRC-16/CCITT over a byte buffer.
[[nodiscard]] inline uint16_t crc16(std::span<const uint8_t> data) {
    uint16_t crc = 0x0000;
    for (uint8_t b : data) {
        crc = crc16_byte(crc, b);
    }
    return crc;
}

/// Compute the LoRa payload CRC as used by the TX chain (add_crc block):
///   1. CRC-16 over the first (payload_len - 2) bytes
///   2. XOR with last byte and (second-to-last byte << 8)
/// Returns the 16-bit CRC to be split into 4 nibbles.
[[nodiscard]] inline uint16_t lora_payload_crc(std::span<const uint8_t> payload) {
    const auto len = payload.size();
    uint16_t   crc = 0x0000;

    // CRC over first (N-2) bytes
    if (len >= 2) {
        crc = crc16(payload.first(len - 2));
        // XOR with last two payload bytes
        crc ^= static_cast<uint16_t>(payload[len - 1]);
        crc ^= static_cast<uint16_t>(payload[len - 2]) << 8;
    }
    return crc;
}

/// Split a 16-bit CRC into 4 nibbles in LoRa order (low byte first, low nibble first).
[[nodiscard]] inline std::array<uint8_t, 4> crc_to_nibbles(uint16_t crc) { return {static_cast<uint8_t>(crc & 0x0F), static_cast<uint8_t>((crc >> 4) & 0x0F), static_cast<uint8_t>((crc >> 8) & 0x0F), static_cast<uint8_t>((crc >> 12) & 0x0F)}; }

/// Verify the LoRa payload CRC on the RX side.
/// payload_with_crc contains payload_len bytes of data followed by 2 CRC bytes (reassembled).
/// Returns true if CRC matches.
[[nodiscard]] inline bool lora_verify_crc(std::span<const uint8_t> payload, uint8_t crc_lo, uint8_t crc_hi) {
    uint16_t received_crc = static_cast<uint16_t>(static_cast<unsigned>(crc_lo) | (static_cast<unsigned>(crc_hi) << 8));
    uint16_t computed     = lora_payload_crc(payload);
    return received_crc == computed;
}

/// Compute the 5-bit explicit header checksum.
/// in[0..4] are the 5 header nibbles (n0=pay_len_hi, n1=pay_len_lo, n2=cr|crc, n3=c4, n4=c3c2c1c0).
/// Returns the 5-bit checksum: {c4, c3, c2, c1, c0} packed as uint8_t.
/// Formula per LoRa explicit header specification (Tapparel & Burg, Section III-A).
[[nodiscard]] inline uint8_t header_checksum(uint8_t n0, uint8_t n1, uint8_t n2) {
    // Extract individual bits (nibble bits numbered 3..0 MSB-first)
    bool a0 = (n0 >> 3) & 1, a1 = (n0 >> 2) & 1, a2 = (n0 >> 1) & 1, a3 = n0 & 1;
    bool a4 = (n1 >> 3) & 1, a5 = (n1 >> 2) & 1, a6 = (n1 >> 1) & 1, a7 = n1 & 1;
    bool a8 = (n2 >> 3) & 1, a9 = (n2 >> 2) & 1, a10 = (n2 >> 1) & 1, a11 = n2 & 1;

    bool c4 = a0 ^ a1 ^ a2 ^ a3;
    bool c3 = a0 ^ a4 ^ a5 ^ a6 ^ a11;
    bool c2 = a1 ^ a4 ^ a7 ^ a8 ^ a10;
    bool c1 = a2 ^ a5 ^ a7 ^ a9 ^ a10 ^ a11;
    bool c0 = a3 ^ a6 ^ a8 ^ a9 ^ a10 ^ a11;

    return static_cast<uint8_t>((c4 << 4) | (c3 << 3) | (c2 << 2) | (c1 << 1) | c0);
}

/// Build the 5-nibble explicit header: [pay_len_hi, pay_len_lo, cr<<1|crc, c4, c3c2c1c0].
[[nodiscard]] inline std::array<uint8_t, 5> build_explicit_header(uint8_t payload_len, uint8_t cr, bool has_crc) {
    uint8_t n0 = (payload_len >> 4) & 0x0F;
    uint8_t n1 = payload_len & 0x0F;
    uint8_t n2 = static_cast<uint8_t>((cr << 1) | (has_crc ? 1 : 0));

    uint8_t chk = header_checksum(n0, n1, n2);
    uint8_t n3  = (chk >> 4) & 0x01; // c4
    uint8_t n4  = chk & 0x0F;        // c3c2c1c0

    return {n0, n1, n2, n3, n4};
}

/// Parse and verify an explicit header from 5 nibbles.
/// Returns: {payload_len, cr, has_crc, checksum_valid}
struct HeaderInfo {
    uint8_t payload_len;
    uint8_t cr;
    bool    has_crc;
    bool    checksum_valid;
};

[[nodiscard]] inline HeaderInfo parse_explicit_header(uint8_t n0, uint8_t n1, uint8_t n2, uint8_t n3, uint8_t n4) {
    uint8_t payload_len = static_cast<uint8_t>((n0 << 4) | n1);
    uint8_t cr          = n2 >> 1;
    bool    has_crc     = n2 & 1;

    // n3 contains only c4 as the low bit: header_chk = ((in[3] & 1) << 4) + in[4]
    uint8_t received_chk = static_cast<uint8_t>(((n3 & 1) << 4) | n4);
    uint8_t computed_chk = header_checksum(n0, n1, n2);

    return {payload_len, cr, has_crc, received_chk == computed_chk};
}

} // namespace gr::lora

#endif // GNURADIO_LORA_CRC_HPP
