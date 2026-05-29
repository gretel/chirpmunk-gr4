// SPDX-License-Identifier: ISC
/// RX algorithm-level tests: walk backwards through TX test vectors to verify
/// each RX stage (GrayMapping, Deinterleave, HammingDec, Header, Dewhitening,
/// CRC) and a full RX pipeline.

#include "test_helpers.hpp"

#include <gnuradio-4.0/lora/algorithm/crc.hpp>
#include <gnuradio-4.0/lora/algorithm/hamming.hpp>
#include <gnuradio-4.0/lora/algorithm/interleaving.hpp>
#include <gnuradio-4.0/lora/algorithm/tables.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>
#include <gnuradio-4.0/lora/algorithm/utilities.hpp>

using namespace gr::lora::test;

// ---- RX Algorithm-Level Tests ----
// Walk backwards through the TX test vectors to verify RX algorithms.

const boost::ut::suite<"GrayMapping RX"> gray_mapping_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "GrayMapping inverts gray_demap (algorithm-level)"_test = [] {
        auto gray_mapped = load_u32("tx_06_gray_mapped.u32");
        auto interleaved = load_u32("tx_05_interleaved.u32");

        // tx_06_gray_mapped = gray_to_binary(interleaved) + 1 mod N   (TX gray_demap)
        // RX FFTDemod: subtracts 1 -> gives gray_to_binary(interleaved)
        // RX GrayMapping: binary_to_gray(x) = x ^ (x >> 1) -> gives interleaved
        for (std::size_t i = 0; i < gray_mapped.size(); i++) {
            uint32_t fft_output = (gray_mapped[i] + N - 1) % N; // simulate FFTDemod -1
            uint16_t rx_gray    = static_cast<uint16_t>(fft_output ^ (fft_output >> 1));
            expect(eq(static_cast<uint32_t>(rx_gray), interleaved[i])) << "GrayMapping mismatch at symbol " << i << ": gray_mapped=" << gray_mapped[i] << " fft_out=" << fft_output << " rx_gray=" << rx_gray << " expected=" << interleaved[i];
        }
    };
};

const boost::ut::suite<"Deinterleaver RX"> deinterleaver_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "Deinterleave full frame (algorithm-level)"_test = [] {
        auto interleaved = load_u32("tx_05_interleaved.u32");
        auto encoded     = load_u8("tx_04_encoded.u8");

        // Process frame block by block, just like the RX chain would
        std::vector<uint8_t> all_codewords;
        std::size_t          sym_idx  = 0;
        bool                 is_first = true;

        while (sym_idx < interleaved.size()) {
            uint8_t sf_app = is_first ? static_cast<uint8_t>(SF - 2) : SF;
            uint8_t cw_len = is_first ? uint8_t(8) : static_cast<uint8_t>(4 + CR);

            if (sym_idx + cw_len > interleaved.size()) {
                break;
            }

            std::vector<uint16_t> block_syms;
            for (std::size_t j = 0; j < cw_len; j++) {
                uint32_t sym = interleaved[sym_idx + j];
                // For header block: FFT demod divides by 2^(SF-sf_app) to strip parity
                if (is_first) {
                    sym >>= (SF - sf_app);
                }
                block_syms.push_back(static_cast<uint16_t>(sym));
            }

            auto codewords = deinterleave_block(block_syms, SF, cw_len, sf_app);
            all_codewords.insert(all_codewords.end(), codewords.begin(), codewords.end());

            sym_idx += cw_len;
            is_first = false;
        }

        // The deinterleaver may produce extra codewords in the last block due to
        // zero-padding. The real RX chain trims based on header payload_len.
        // We just verify the first encoded.size() codewords match.
        expect(ge(all_codewords.size(), encoded.size())) << "Deinterleave frame size: got " << all_codewords.size() << " expected at least " << encoded.size();
        for (std::size_t i = 0; i < encoded.size(); i++) {
            expect(eq(all_codewords[i], encoded[i])) << "Deinterleave frame mismatch at codeword " << i;
        }
    };
};

const boost::ut::suite<"HammingDec RX"> hamming_dec_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "HammingDec vs test vector (algorithm-level)"_test = [] {
        auto encoded  = load_u8("tx_04_encoded.u8");
        auto with_crc = load_u8("tx_03_with_crc.u8");

        // Decode all codewords. First sf-2 use cr_app=4 (header), rest use CR.
        std::vector<uint8_t> decoded;
        for (std::size_t i = 0; i < encoded.size(); i++) {
            uint8_t cr_app = (static_cast<int>(i) < SF - 2) ? uint8_t(4) : CR;
            decoded.push_back(hamming_decode_hard(encoded[i], cr_app));
        }

        expect(eq(decoded.size(), with_crc.size())) << "HammingDec size: got " << decoded.size() << " expected " << with_crc.size();
        for (std::size_t i = 0; i < decoded.size() && i < with_crc.size(); i++) {
            expect(eq(decoded[i], with_crc[i])) << "HammingDec mismatch at nibble " << i;
        }
    };
};

const boost::ut::suite<"HeaderDecoder RX"> header_decoder_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "HeaderDecoder parses explicit header (algorithm-level)"_test = [] {
        auto with_crc    = load_u8("tx_03_with_crc.u8");
        auto payload_str = load_text("payload.txt");

        // First 5 nibbles are the header
        expect(ge(with_crc.size(), std::size_t{5})) << "Not enough nibbles for header";

        auto info = parse_explicit_header(with_crc[0], with_crc[1], with_crc[2], with_crc[3], with_crc[4]);

        expect(eq(static_cast<int>(info.payload_len), static_cast<int>(payload_str.size()))) << "Header payload_len: got " << info.payload_len << " expected " << payload_str.size();
        expect(eq(static_cast<int>(info.cr), static_cast<int>(CR))) << "Header CR: got " << info.cr << " expected " << CR;
        expect(info.has_crc == HAS_CRC) << "Header CRC flag mismatch";
        expect(info.checksum_valid) << "Header checksum invalid";
    };
};

const boost::ut::suite<"Dewhitening RX"> dewhitening_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "Dewhitening vs test vector (algorithm-level)"_test = [] {
        auto with_crc    = load_u8("tx_03_with_crc.u8");
        auto whitened    = load_u8("tx_01_whitened_nibbles.u8");
        auto payload_str = load_text("payload.txt");
        auto payload_len = payload_str.size();

        // Strip the 5 header nibbles
        std::size_t payload_nibs = payload_len * 2;
        expect(ge(with_crc.size(), 5 + payload_nibs + 4)) << "tx_03_with_crc too small";

        // Verify whitened nibbles match
        for (std::size_t i = 0; i < payload_nibs && i < whitened.size(); i++) {
            expect(eq(with_crc[5 + i], whitened[i])) << "Whitened nibble mismatch at " << i;
        }

        // Now dewhiten: pair up nibbles, XOR with LFSR, reassemble bytes
        std::vector<uint8_t> dewhitened_bytes;
        for (std::size_t i = 0; i < payload_nibs / 2; i++) {
            uint8_t low_nib  = with_crc[5 + 2 * i];
            uint8_t high_nib = with_crc[5 + 2 * i + 1];

            // Dewhiten
            uint8_t ws = whitening_seq[i % whitening_seq.size()];
            low_nib ^= (ws & 0x0F);
            high_nib ^= ((ws >> 4) & 0x0F);

            dewhitened_bytes.push_back(static_cast<uint8_t>((high_nib << 4) | low_nib));
        }

        expect(eq(dewhitened_bytes.size(), payload_len)) << "Dewhitened size mismatch";
        for (std::size_t i = 0; i < dewhitened_bytes.size(); i++) {
            expect(eq(dewhitened_bytes[i], static_cast<uint8_t>(payload_str[i]))) << "Dewhitened byte mismatch at " << i << ": got 0x" << std::format("{:02x}", dewhitened_bytes[i]) << " expected 0x" << std::format("{:02x}", static_cast<uint8_t>(payload_str[i]));
        }
    };
};

const boost::ut::suite<"CRC Verify RX"> crc_verif_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "CRC verification on decoded payload (algorithm-level)"_test = [] {
        auto payload_str = load_text("payload.txt");
        auto rx_payload  = load_u8("rx_decoded_payload.bin");
        auto rx_crc_flag = load_u8("rx_crc_flags.u8");

        // Verify that rx_decoded_payload matches the original payload
        expect(eq(rx_payload.size(), payload_str.size())) << "RX payload size: " << rx_payload.size() << " expected " << payload_str.size();
        for (std::size_t i = 0; i < rx_payload.size() && i < payload_str.size(); i++) {
            expect(eq(rx_payload[i], static_cast<uint8_t>(payload_str[i]))) << "RX payload mismatch at byte " << i;
        }

        // Verify CRC flag
        expect(!rx_crc_flag.empty()) << "CRC flag file empty";
        if (!rx_crc_flag.empty()) {
            expect(eq(rx_crc_flag[0], uint8_t{1})) << "CRC flag should be valid (1)";
        }
    };

    "CRC-16 computation matches test vector nibbles (algorithm-level)"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        expect(ge(payload.size(), 2UZ)) << "payload must be >= 2 bytes for LoRa CRC";

        // Compute LoRa payload CRC using the same function as the decoder.
        const uint16_t expected_crc = lora_payload_crc(std::span<const uint8_t>(payload.data(), payload.size()));

        // Read the (whitened) CRC nibbles from the test vector.
        auto with_crc = load_u8("tx_03_with_crc.u8");
        expect(ge(with_crc.size(), 4UZ)) << "tx_03_with_crc.u8 too small";
        const std::size_t crc_start = with_crc.size() - 4UZ;
        std::array<uint8_t, 4> nibs{with_crc[crc_start], with_crc[crc_start + 1UZ], with_crc[crc_start + 2UZ], with_crc[crc_start + 3UZ]};

        // Dewhiten CRC nibbles: whitening sequence continues from payload.
        for (std::size_t byte_idx = 0u; byte_idx < 2u; ++byte_idx) {
            const uint8_t ws = whitening_seq[(payload.size() + byte_idx) % whitening_seq.size()];
            nibs[byte_idx * 2u] ^= (ws & 0x0F);
            nibs[byte_idx * 2u + 1u] ^= ((ws >> 4u) & 0x0F);
        }

        const uint16_t dewhitened_crc = static_cast<uint16_t>(static_cast<unsigned>(nibs[0]) | (static_cast<unsigned>(nibs[1]) << 4u) | (static_cast<unsigned>(nibs[2]) << 8u) | (static_cast<unsigned>(nibs[3]) << 12u));

        expect(eq(expected_crc, dewhitened_crc)) << "CRC mismatch: computed=0x" << std::format("{:04x}", expected_crc) << " from_nibs=0x" << std::format("{:04x}", dewhitened_crc);
    };
};

// ---- Full RX Algorithm Pipeline ----

const boost::ut::suite<"Full RX pipeline (algorithm-level)"> full_rx_pipeline_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "Full RX: gray_mapped -> payload (algorithm-level)"_test = [] {
        auto gray_mapped = load_u32("tx_06_gray_mapped.u32");
        auto payload_str = load_text("payload.txt");

        // === Stage 1: FFTDemod simulation (subtract 1) ===
        std::vector<uint16_t> fft_output;
        for (auto gm : gray_mapped) {
            fft_output.push_back(static_cast<uint16_t>((gm + N - 1) % N));
        }

        // === Stage 2: GrayMapping (binary_to_gray) ===
        std::vector<uint16_t> interleaved;
        for (auto sym : fft_output) {
            interleaved.push_back(static_cast<uint16_t>(sym ^ (sym >> 1)));
        }

        // === Stage 3: Deinterleave ===
        std::vector<uint8_t> all_codewords;
        std::size_t          sym_idx  = 0;
        bool                 is_first = true;
        while (sym_idx < interleaved.size()) {
            uint8_t sf_app = is_first ? static_cast<uint8_t>(SF - 2) : SF;
            uint8_t cw_len = is_first ? uint8_t(8) : static_cast<uint8_t>(4 + CR);

            if (sym_idx + cw_len > interleaved.size()) {
                break;
            }

            std::vector<uint16_t> block_syms;
            for (std::size_t j = 0; j < cw_len; j++) {
                uint16_t sym = interleaved[sym_idx + j];
                if (is_first) {
                    sym >>= (SF - sf_app); // strip parity/zero for header
                }
                block_syms.push_back(sym);
            }

            auto cw = deinterleave_block(block_syms, SF, cw_len, sf_app);
            all_codewords.insert(all_codewords.end(), cw.begin(), cw.end());

            sym_idx += cw_len;
            is_first = false;
        }

        // === Stage 4: Hamming decode ===
        std::vector<uint8_t> nibbles;
        for (std::size_t i = 0; i < all_codewords.size(); i++) {
            uint8_t cr_app = (static_cast<int>(i) < SF - 2) ? uint8_t(4) : CR;
            nibbles.push_back(hamming_decode_hard(all_codewords[i], cr_app));
        }

        // === Stage 5: Header decode ===
        expect(ge(nibbles.size(), std::size_t{5})) << "Not enough nibbles for header";
        auto hdr = parse_explicit_header(nibbles[0], nibbles[1], nibbles[2], nibbles[3], nibbles[4]);
        expect(hdr.checksum_valid) << "Header checksum invalid";
        expect(eq(static_cast<int>(hdr.payload_len), static_cast<int>(payload_str.size()))) << "Header payload_len mismatch";
        expect(eq(static_cast<int>(hdr.cr), static_cast<int>(CR))) << "Header CR mismatch";
        expect(hdr.has_crc == HAS_CRC) << "Header CRC flag mismatch";

        // === Stage 6: Dewhitening ===
        std::size_t payload_nibs = hdr.payload_len * 2;
        std::size_t crc_nibs     = hdr.has_crc ? 4 : 0;
        std::size_t data_start   = 5; // skip header nibbles

        std::vector<uint8_t> decoded_bytes;
        for (std::size_t i = 0; i < (payload_nibs + crc_nibs) / 2; i++) {
            std::size_t nib_idx = data_start + 2 * i;
            if (nib_idx + 1 >= nibbles.size()) {
                break;
            }

            uint8_t low_nib  = nibbles[nib_idx];
            uint8_t high_nib = nibbles[nib_idx + 1];

            uint8_t ws = whitening_seq[i % whitening_seq.size()];
            low_nib ^= (ws & 0x0F);
            high_nib ^= ((ws >> 4) & 0x0F);

            decoded_bytes.push_back(static_cast<uint8_t>((high_nib << 4) | low_nib));
        }

        // === Stage 7: CRC verify ===
        expect(ge(decoded_bytes.size(), static_cast<std::size_t>(hdr.payload_len) + 2)) << "Not enough decoded bytes for CRC check: " << decoded_bytes.size();

        if (hdr.has_crc && hdr.payload_len >= 2 && decoded_bytes.size() >= hdr.payload_len + 2u) {
            uint16_t computed_crc = lora_payload_crc(std::span<const uint8_t>(decoded_bytes.data(), hdr.payload_len));

            uint16_t received_crc = static_cast<uint16_t>(static_cast<unsigned>(decoded_bytes[hdr.payload_len]) | (static_cast<unsigned>(decoded_bytes[hdr.payload_len + 1]) << 8));

            expect(eq(computed_crc, received_crc)) << "CRC mismatch: computed=0x" << std::format("{:04x}", computed_crc) << " received=0x" << std::format("{:04x}", received_crc);
        }

        // === Final: compare payload ===
        std::string decoded_str(decoded_bytes.begin(), decoded_bytes.begin() + hdr.payload_len);
        expect(eq(decoded_str, payload_str)) << "Payload mismatch: got \"" << decoded_str << "\" expected \"" << payload_str << "\"";
    };
};

// ============================================================================
// Multi-config RX pipeline: verify all 36 SF×CR×BW configurations
// ============================================================================

const boost::ut::suite<"Multi-config RX pipeline"> multi_config_rx_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;
    using namespace gr::lora::test;

    "multi-config RX pipeline"_test = [] {
        for (const auto& cfg : allConfigs()) {
            const auto     label    = cfg.subdir();
            const bool     use_ldro = needs_ldro(cfg.sf, cfg.bw);
            const uint32_t N_val    = cfg.N();

            auto gray_mapped = load_u32(cfg, "tx_06_gray_mapped.u32");
            auto payload_str = load_text(cfg, "payload.txt");

            // === Stage 1: FFTDemod simulation (subtract 1 mod N) ===
            std::vector<uint16_t> fft_output;
            for (auto gm : gray_mapped) {
                fft_output.push_back(static_cast<uint16_t>((gm + N_val - 1) % N_val));
            }

            // === Stage 2: GrayMapping (binary_to_gray) ===
            std::vector<uint16_t> interleaved;
            for (auto sym : fft_output) {
                interleaved.push_back(static_cast<uint16_t>(sym ^ (sym >> 1)));
            }

            // === Stage 3: Deinterleave (LDRO-aware) ===
            std::vector<uint8_t> all_codewords;
            std::size_t          sym_idx  = 0;
            bool                 is_first = true;
            while (sym_idx < interleaved.size()) {
                bool    reduced_rate = is_first || use_ldro;
                uint8_t sf_app       = reduced_rate ? static_cast<uint8_t>(cfg.sf - 2) : cfg.sf;
                uint8_t cw_len       = is_first ? uint8_t(8) : static_cast<uint8_t>(4 + cfg.cr);

                if (sym_idx + cw_len > interleaved.size()) {
                    break;
                }

                std::vector<uint16_t> block_syms;
                for (std::size_t j = 0; j < cw_len; j++) {
                    uint16_t sym = interleaved[sym_idx + j];
                    if (reduced_rate) {
                        sym >>= (cfg.sf - sf_app);
                    }
                    block_syms.push_back(sym);
                }

                auto cw = deinterleave_block(block_syms, cfg.sf, cw_len, sf_app);
                all_codewords.insert(all_codewords.end(), cw.begin(), cw.end());

                sym_idx += cw_len;
                is_first = false;
            }

            // === Stage 4: Hamming decode ===
            std::vector<uint8_t> nibbles;
            for (std::size_t i = 0; i < all_codewords.size(); i++) {
                uint8_t cr_app = (static_cast<int>(i) < cfg.sf - 2) ? uint8_t(4) : cfg.cr;
                nibbles.push_back(hamming_decode_hard(all_codewords[i], cr_app));
            }

            // === Stage 5: Header decode ===
            expect(ge(nibbles.size(), std::size_t{5})) << label << " not enough nibbles for header";
            auto hdr = parse_explicit_header(nibbles[0], nibbles[1], nibbles[2], nibbles[3], nibbles[4]);
            expect(hdr.checksum_valid) << label << " header checksum invalid";
            expect(eq(static_cast<int>(hdr.payload_len), static_cast<int>(payload_str.size()))) << label << " header payload_len mismatch";
            expect(eq(static_cast<int>(hdr.cr), static_cast<int>(cfg.cr))) << label << " header CR mismatch";

            // === Stage 6: Dewhitening via dewhiten() ===
            std::size_t          data_nibs = hdr.payload_len * 2 + (hdr.has_crc ? 4 : 0);
            std::vector<uint8_t> data_nibbles(nibbles.begin() + 5, nibbles.begin() + static_cast<std::ptrdiff_t>(std::min(nibbles.size(), 5 + data_nibs)));
            auto                 decoded_bytes = dewhiten(data_nibbles, hdr.payload_len);

            // Dewhiten CRC bytes: SX1262 whitens entire data stream including CRC.
            // dewhiten() only dewhitens the first pay_len payload bytes; CRC bytes
            // at index pay_len and pay_len+1 need the same per-byte XOR here.
            if (decoded_bytes.size() >= hdr.payload_len + 2u) {
                decoded_bytes[hdr.payload_len] ^= whitening_seq[hdr.payload_len % whitening_seq.size()];
                decoded_bytes[hdr.payload_len + 1u] ^= whitening_seq[(hdr.payload_len + 1u) % whitening_seq.size()];
            }

            // === Stage 7: CRC verify ===
            if (hdr.has_crc && decoded_bytes.size() >= hdr.payload_len + 2u) {
                std::span<const uint8_t> pay_span(decoded_bytes.data(), hdr.payload_len);
                bool                     crc_ok = lora_verify_crc(pay_span, decoded_bytes[hdr.payload_len], decoded_bytes[hdr.payload_len + 1]);
                expect(crc_ok) << label << " CRC verification failed";
            }

            // === Final: compare payload ===
            std::string decoded_str(decoded_bytes.begin(), decoded_bytes.begin() + hdr.payload_len);
            expect(eq(decoded_str, payload_str)) << label << " payload mismatch: got \"" << decoded_str << "\" expected \"" << payload_str << "\"";
        }
    };
};

int main() { /* boost::ut auto-runs all suites */ }
