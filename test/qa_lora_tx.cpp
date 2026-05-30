// SPDX-License-Identifier: ISC
/// TX chain unit tests: each tx_chain.hpp function tested in isolation
/// against GR3 test vectors, plus generate_frame_iq() integration tests.

#include "test_helpers.hpp"

#include <algorithm>
#include <cmath>

#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>

using namespace gr::lora::test;

// ============================================================================
// Per-stage TX tests vs GR3 test vectors
// ============================================================================

const boost::ut::suite<"TX whiten"> tx_whiten_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "whiten() vs test vector"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        auto whitened = whiten(payload);
        auto expected = load_u8("tx_01_whitened_nibbles.u8");

        expect(eq(whitened.size(), expected.size())) << "whitened size";
        for (std::size_t i = 0; i < whitened.size(); i++) {
            expect(eq(whitened[i], expected[i])) << "whitened[" << i << "]";
        }
    };

    "whiten() empty payload"_test = [] {
        std::vector<uint8_t> empty;
        auto                 result = whiten(empty);
        expect(eq(result.size(), 0UZ)) << "empty payload should produce 0 nibbles";
    };

    "whiten() single byte"_test = [] {
        std::vector<uint8_t> one    = {0x42};
        auto                 result = whiten(one);
        expect(eq(result.size(), 2UZ)) << "1-byte payload should produce 2 nibbles";
        // Verify manually: byte 0x42 = low_nib 0x2, high_nib 0x4
        // XOR with whitening_seq[0] = 0xFF -> low = 0x2^0xF = 0xD, high = 0x4^0xF = 0xB
        uint8_t ws = whitening_seq[0];
        expect(eq(result[0], static_cast<uint8_t>((0x42 & 0x0F) ^ (ws & 0x0F)))) << "low nibble";
        expect(eq(result[1], static_cast<uint8_t>(((0x42 >> 4) & 0x0F) ^ ((ws >> 4) & 0x0F)))) << "high nibble";
    };
};

const boost::ut::suite<"TX insert_header"> tx_header_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "insert_header() vs test vector"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        auto whitened    = whiten(payload);
        auto with_header = insert_header(whitened, static_cast<uint8_t>(payload.size()), CR, HAS_CRC);
        auto expected    = load_u8("tx_02_with_header.u8");

        expect(eq(with_header.size(), expected.size())) << "header+payload size";
        for (std::size_t i = 0; i < with_header.size(); i++) {
            expect(eq(with_header[i], expected[i])) << "header[" << i << "]";
        }
    };

    "insert_header() prepends exactly 5 nibbles"_test = [] {
        std::vector<uint8_t> nibbles = {0x1, 0x2, 0x3, 0x4};
        auto                 result  = insert_header(nibbles, 2, CR, HAS_CRC);
        expect(eq(result.size(), 5UZ + nibbles.size())) << "should be 5 header + original nibbles";
        // Tail should be the original nibbles
        for (std::size_t i = 0; i < nibbles.size(); i++) {
            expect(eq(result[5 + i], nibbles[i])) << "payload nibble preserved at [" << i << "]";
        }
    };
};

const boost::ut::suite<"TX add_crc"> tx_add_crc_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "add_crc() vs test vector"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        auto whitened    = whiten(payload);
        auto with_header = insert_header(whitened, static_cast<uint8_t>(payload.size()), CR, HAS_CRC);
        auto with_crc    = add_crc(with_header, payload, HAS_CRC);
        auto expected    = load_u8("tx_03_with_crc.u8");

        expect(eq(with_crc.size(), expected.size())) << "with_crc size";
        for (std::size_t i = 0; i < with_crc.size(); i++) {
            expect(eq(with_crc[i], expected[i])) << "with_crc[" << i << "]";
        }
    };

    "add_crc() with has_crc=false appends nothing"_test = [] {
        std::vector<uint8_t> nibbles = {0x1, 0x2, 0x3, 0x4, 0x5};
        std::vector<uint8_t> payload = {0xAA, 0xBB, 0xCC};
        auto                 result  = add_crc(nibbles, payload, false);
        expect(eq(result.size(), nibbles.size())) << "has_crc=false should not append anything";
        for (std::size_t i = 0; i < nibbles.size(); i++) {
            expect(eq(result[i], nibbles[i]));
        }
    };

    "add_crc() with 1-byte payload appends 4 nibbles (CRC-16/XMODEM fallback)"_test = [] {
        // Payload < 2 bytes uses standard CRC-16/XMODEM (XOR variant
        // needs ≥2 bytes for the tail XOR). Verify 4 nibbles are appended
        // and the first nibbles are unchanged.
        std::vector<uint8_t> nibbles = {0x1, 0x2, 0x3, 0x4, 0x5, 0xA, 0xB};
        std::vector<uint8_t> payload = {0x42};
        auto                 result  = add_crc(nibbles, payload, true);
        expect(eq(result.size(), nibbles.size() + 4u)) << "1-byte payload: 4 CRC nibbles (CRC-16/XMODEM fallback)";
        // First nibbles unchanged
        for (std::size_t i = 0; i < nibbles.size(); i++) {
            expect(eq(result[i], nibbles[i]));
        }
        // CRC nibbles are non-zero (CRC-16/XMODEM of {0x42} != 0)
        expect(result[7] != 0 || result[8] != 0 || result[9] != 0 || result[10] != 0) << "CRC should be non-zero";
    };

    "add_crc() appends exactly 4 nibbles"_test = [] {
        std::vector<uint8_t> nibbles = {0x1, 0x2, 0x3};
        std::vector<uint8_t> payload = {0xAA, 0xBB, 0xCC};
        auto                 result  = add_crc(nibbles, payload, true);
        expect(eq(result.size(), nibbles.size() + 4)) << "should append exactly 4 CRC nibbles";
        // Verify the first 3 nibbles are unchanged
        for (std::size_t i = 0; i < nibbles.size(); i++) {
            expect(eq(result[i], nibbles[i]));
        }
    };
};

const boost::ut::suite<"TX hamming_encode_frame"> tx_hamming_enc_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "hamming_encode_frame() vs test vector"_test = [] {
        auto with_crc = load_u8("tx_03_with_crc.u8");
        auto encoded  = hamming_encode_frame(with_crc, SF, CR);
        auto expected = load_u8("tx_04_encoded.u8");

        expect(eq(encoded.size(), expected.size())) << "encoded size";
        for (std::size_t i = 0; i < encoded.size(); i++) {
            expect(eq(encoded[i], expected[i])) << "encoded[" << i << "]";
        }
    };

    "hamming_encode_frame() first sf-2 nibbles use cr=4"_test = [] {
        // Verify that the first sf-2 codewords match cr=4 encoding
        // regardless of the frame CR setting (even if CR=1)
        std::vector<uint8_t> nibbles     = {0xA, 0xB, 0xC, 0xD, 0xE, 0xF, 0x1, 0x2};
        auto                 encoded_cr1 = hamming_encode_frame(nibbles, SF, 1);
        auto                 encoded_cr4 = hamming_encode_frame(nibbles, SF, 4);

        // First sf-2=6 codewords should be identical (both use cr=4)
        for (int i = 0; i < SF - 2; i++) {
            expect(eq(encoded_cr1[static_cast<std::size_t>(i)], encoded_cr4[static_cast<std::size_t>(i)])) << "header codeword[" << i << "] should use cr=4 regardless";
        }
        // Codewords after the header region differ because cr=1 vs cr=4
        // produce different codeword widths (5 vs 8 bits)
        // Just verify they exist
        expect(eq(encoded_cr1.size(), nibbles.size()));
        expect(eq(encoded_cr4.size(), nibbles.size()));
    };
};

const boost::ut::suite<"TX interleave_frame"> tx_interleave_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "interleave_frame() vs test vector"_test = [] {
        auto encoded  = load_u8("tx_04_encoded.u8");
        auto result   = interleave_frame(encoded, SF, CR);
        auto expected = load_u32("tx_05_interleaved.u32");

        expect(eq(result.size(), expected.size())) << "interleaved size";
        for (std::size_t i = 0; i < result.size(); i++) {
            expect(eq(result[i], expected[i])) << "interleaved[" << i << "]";
        }
    };

    "interleave_frame() LDRO produces more symbols"_test = [] {
        // When LDRO is active, payload blocks use sf_app=sf-2, requiring
        // more blocks to encode the same data (lower spectral efficiency).
        auto encoded = load_u8("tx_04_encoded.u8");
        auto normal  = interleave_frame(encoded, SF, CR, false);
        auto ldro    = interleave_frame(encoded, SF, CR, true);

        expect(ge(ldro.size(), normal.size())) << "LDRO should produce >= symbols: ldro=" << ldro.size() << " normal=" << normal.size();
    };

    "interleave_frame() LDRO roundtrip at SF11 BW125kHz"_test = [] {
        // SF11 @ BW=125kHz: symbol_duration = 2048*1000/125000 = 16.384ms > 16ms -> LDRO
        constexpr uint8_t sf11 = 11;
        // Create some test nibbles: 5 header + payload nibbles
        std::vector<uint8_t> codewords;
        for (int i = 0; i < 30; i++) {
            codewords.push_back(static_cast<uint8_t>(i & 0xFF));
        }

        auto symbols = interleave_frame(codewords, sf11, CR, true);

        // Header block: cw_len=8, sf_app=sf-2=9 -> 8 symbols consuming 9 codewords
        // Payload blocks: cw_len=4+CR=8, sf_app=sf-2=9 -> 8 symbols consuming 9 codewords each
        // Total codewords: 30, header consumes 9, remaining 21 -> ceil(21/9)=3 payload blocks
        // Total symbols: 8 (header) + 3*8 (payload) = 32
        expect(eq(symbols.size(), 32UZ)) << "SF11 LDRO: expected 32 symbols, got " << symbols.size();

        // Verify all symbols are in range [0, 2^sf)
        uint32_t n11 = 1u << sf11;
        for (std::size_t i = 0; i < symbols.size(); i++) {
            expect(lt(symbols[i], n11)) << "symbol[" << i << "] out of range";
        }
    };
};

const boost::ut::suite<"TX gray_demap"> tx_gray_demap_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "gray_demap() vs test vector"_test = [] {
        auto interleaved = load_u32("tx_05_interleaved.u32");
        auto result      = gray_demap(interleaved, SF);
        auto expected    = load_u32("tx_06_gray_mapped.u32");

        expect(eq(result.size(), expected.size())) << "gray_demap size";
        for (std::size_t i = 0; i < result.size(); i++) {
            expect(eq(result[i], expected[i])) << "gray_demap[" << i << "]";
        }
    };

    "gray_demap() boundary values"_test = [] {
        // symbol 0 -> gray_to_binary(0) + 1 = 0 + 1 = 1
        std::vector<uint32_t> sym0 = {0};
        auto                  r0   = gray_demap(sym0, SF);
        expect(eq(r0[0], 1u)) << "symbol 0 -> 1";

        // symbol N-1=255 -> gray_to_binary(255) + 1 mod 256
        std::vector<uint32_t> symN = {N - 1};
        auto                  rN   = gray_demap(symN, SF);
        // gray_to_binary(255) for 8 bits: 255 ^ 127 ^ 63 ^ 31 ^ 15 ^ 7 ^ 3 ^ 1 = 170
        // (255 + 1) mod 256 would be wrong — it's gray decode + 1
        // Verify it's in range [0, N)
        expect(lt(rN[0], N)) << "result in range";
    };

    "gray_demap() empty input"_test = [] {
        std::vector<uint32_t> empty;
        auto                  result = gray_demap(empty, SF);
        expect(eq(result.size(), 0UZ));
    };
};

const boost::ut::suite<"TX modulate_frame"> tx_modulate_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "modulate_frame() IQ vs GR3 test vector"_test = [] {
        auto gray_mapped = load_u32("tx_06_gray_mapped.u32");
        auto expected_iq = load_cf32("tx_07_iq_frame.cf32");

        constexpr uint32_t ZERO_PAD = SPS * 5;
        auto               iq       = modulate_frame(gray_mapped, SF, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, ZERO_PAD);

        expect(eq(iq.size(), expected_iq.size())) << "IQ size: got " << iq.size() << " expected " << expected_iq.size();

        // Compare preamble + SFD region
        std::size_t preamble_end = static_cast<std::size_t>((PREAMBLE_LEN + 4.25) * SPS);
        float       max_err_pre  = 0.f;
        for (std::size_t i = 0; i < preamble_end && i < iq.size(); i++) {
            float err = std::max(std::abs(iq[i].real() - expected_iq[i].real()), std::abs(iq[i].imag() - expected_iq[i].imag()));
            if (err > max_err_pre) {
                max_err_pre = err;
            }
        }
        expect(lt(max_err_pre, 1e-4f)) << "Preamble+SFD max error: " << max_err_pre;

        // Compare each payload symbol
        for (std::size_t s = 0; s < gray_mapped.size(); s++) {
            std::size_t sym_start   = preamble_end + s * SPS;
            float       sym_max_err = 0.f;
            for (std::size_t j = 0; j < SPS && sym_start + j < iq.size(); j++) {
                float err = std::max(std::abs(iq[sym_start + j].real() - expected_iq[sym_start + j].real()), std::abs(iq[sym_start + j].imag() - expected_iq[sym_start + j].imag()));
                if (err > sym_max_err) {
                    sym_max_err = err;
                }
            }
            expect(lt(sym_max_err, 1e-4f)) << "Payload symbol " << s << " max error: " << sym_max_err;
        }
    };

    "modulate_frame() zero_pad=0 produces correct size"_test = [] {
        std::vector<uint32_t> symbols = {42, 100};
        auto                  iq      = modulate_frame(symbols, SF, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, 0);
        // Expected: (8 preamble + 2 sync + 2.25 SFD + 2 payload) * SPS
        std::size_t expected = static_cast<std::size_t>((PREAMBLE_LEN + 4.25) * SPS) + 2 * SPS;
        expect(eq(iq.size(), expected)) << "size with zero_pad=0: " << iq.size() << " expected " << expected;
    };

    "modulate_frame() all samples are unit magnitude (payload)"_test = [] {
        std::vector<uint32_t> symbols = {0, 127, 255};
        auto                  iq      = modulate_frame(symbols, SF, 1, SYNC_WORD, PREAMBLE_LEN, 0);
        // Skip zero-padding region (there isn't one). Check payload chirps only.
        std::size_t payload_start = static_cast<std::size_t>((PREAMBLE_LEN + 4.25) * N);
        float       max_err       = 0.f;
        for (std::size_t i = payload_start; i < iq.size(); i++) {
            float mag = std::abs(iq[i]);
            float err = std::abs(mag - 1.0f);
            if (err > max_err) {
                max_err = err;
            }
        }
        expect(lt(max_err, 1e-5f)) << "payload chirps unit magnitude err: " << max_err;
    };
};

// ============================================================================
// generate_frame_iq() integration tests
// ============================================================================

const boost::ut::suite<"TX generate_frame_iq"> tx_generate_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "generate_frame_iq() matches GR3 IQ test vector"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());
        auto                 expected_iq = load_cf32("tx_07_iq_frame.cf32");

        constexpr uint32_t ZERO_PAD = SPS * 5;
        auto               iq       = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, ZERO_PAD);

        expect(eq(iq.size(), expected_iq.size())) << "generate_frame_iq size: " << iq.size() << " expected " << expected_iq.size();

        float max_err = 0.f;
        for (std::size_t i = 0; i < iq.size() && i < expected_iq.size(); i++) {
            float err = std::max(std::abs(iq[i].real() - expected_iq[i].real()), std::abs(iq[i].imag() - expected_iq[i].imag()));
            if (err > max_err) {
                max_err = err;
            }
        }
        expect(lt(max_err, 1e-4f)) << "generate_frame_iq max sample error: " << max_err;
    };

    "generate_frame_iq() with has_crc=false produces shorter IQ"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        auto iq_crc   = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, true, 0);
        auto iq_nocrc = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, false, 0);

        // Without CRC, the frame has fewer nibbles -> fewer codewords -> fewer symbols
        // -> fewer IQ samples (or equal — depends on interleaver padding)
        expect(le(iq_nocrc.size(), iq_crc.size())) << "no-CRC frame should be <= CRC frame: " << iq_nocrc.size() << " vs " << iq_crc.size();
    };

    "generate_frame_iq() with zero_pad=0 has no trailing silence"_test = [] {
        auto                 payload_str = load_text("payload.txt");
        std::vector<uint8_t> payload(payload_str.begin(), payload_str.end());

        auto iq = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, 0);

        // Last sample should be a chirp sample (non-zero magnitude)
        if (!iq.empty()) {
            float last_mag = std::abs(iq.back());
            expect(gt(last_mag, 0.5f)) << "last sample should be a chirp (mag > 0.5): " << last_mag;
        }
    };

    "generate_frame_iq() with zero_pad has trailing zeros"_test = [] {
        std::vector<uint8_t> payload = {'H', 'i'};
        constexpr uint32_t   PAD     = 512;
        auto                 iq      = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, PAD);

        // Last PAD samples should be zero
        bool all_zero = true;
        for (std::size_t i = iq.size() - PAD; i < iq.size(); i++) {
            if (iq[i] != std::complex<float>(0.f, 0.f)) {
                all_zero = false;
                break;
            }
        }
        expect(all_zero) << "trailing " << PAD << " samples should be zero";
    };
};

// ============================================================================
// Inverted IQ (downchirp) TX tests
// ============================================================================

const boost::ut::suite<"TX inverted IQ"> tx_inverted_iq_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;

    "inverted IQ is conjugate of normal IQ"_test = [] {
        std::vector<uint8_t> payload     = {'H', 'i'};
        auto                 iq_normal   = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, 0, 2, 62500, false);
        auto                 iq_inverted = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, 0, 2, 62500, true);

        expect(eq(iq_normal.size(), iq_inverted.size())) << "Normal and inverted IQ should be same size";

        // Inverted IQ should be the conjugate of normal IQ
        float max_err = 0.f;
        for (std::size_t i = 0; i < iq_normal.size(); i++) {
            auto  conj_normal = std::conj(iq_normal[i]);
            float err         = std::max(std::abs(conj_normal.real() - iq_inverted[i].real()), std::abs(conj_normal.imag() - iq_inverted[i].imag()));
            if (err > max_err) {
                max_err = err;
            }
        }
        expect(lt(max_err, 1e-5f)) << "Inverted IQ should equal conj(normal): max err " << max_err;
    };

    "inverted IQ preamble starts with downchirps"_test = [] {
        std::vector<uint8_t> payload = {'X'};
        auto                 iq      = generate_frame_iq(payload, SF, CR, OS_FACTOR, SYNC_WORD, PREAMBLE_LEN, HAS_CRC, 0, 2, 62500, true);

        // Build reference downchirp
        std::vector<std::complex<float>> upchirp(SPS), downchirp(SPS);
        build_ref_chirps(upchirp.data(), downchirp.data(), SF, OS_FACTOR);

        // First SPS samples should be a downchirp (first preamble symbol)
        float max_err = 0.f;
        for (std::size_t i = 0; i < SPS && i < iq.size(); i++) {
            float err = std::max(std::abs(iq[i].real() - downchirp[i].real()), std::abs(iq[i].imag() - downchirp[i].imag()));
            if (err > max_err) {
                max_err = err;
            }
        }
        expect(lt(max_err, 1e-5f)) << "Inverted preamble should start with downchirps: max err " << max_err;
    };
};

// ============================================================================
// Multi-config TX pipeline: verify all 36 SF×CR×BW configurations
// ============================================================================

const boost::ut::suite<"TX multi-config pipeline"> tx_multi_config_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;
    using namespace gr::lora::test;

    "multi-config TX pipeline"_test = [] {
        for (const auto& cfg : allConfigs()) {
            const auto                 label = cfg.subdir();
            const std::vector<uint8_t> payload(cfg.payload.begin(), cfg.payload.end());
            const bool                 use_ldro = needs_ldro(cfg.sf, cfg.bw);

            auto whitened = whiten(payload);
            expect_vectors_equal(whitened, load_u8(cfg, "tx_01_whitened_nibbles.u8"), label + " whiten");

            auto with_header = insert_header(whitened, static_cast<uint8_t>(payload.size()), cfg.cr, cfg.has_crc);
            expect_vectors_equal(with_header, load_u8(cfg, "tx_02_with_header.u8"), label + " header");

            auto with_crc = add_crc(with_header, payload, cfg.has_crc);
            expect_vectors_equal(with_crc, load_u8(cfg, "tx_03_with_crc.u8"), label + " crc");

            auto encoded = hamming_encode_frame(with_crc, cfg.sf, cfg.cr);
            expect_vectors_equal(encoded, load_u8(cfg, "tx_04_encoded.u8"), label + " hamming");

            auto interleaved = interleave_frame(encoded, cfg.sf, cfg.cr, use_ldro);
            expect_vectors_equal(interleaved, load_u32(cfg, "tx_05_interleaved.u32"), label + " interleave");

            auto gray = gray_demap(interleaved, cfg.sf);
            expect_vectors_equal(gray, load_u32(cfg, "tx_06_gray_mapped.u32"), label + " gray");
        }
    };
};

// ============================================================================
// Burst taper — applied by apps/lora_trx.cpp generate_iq before SoapySink
// ============================================================================

#include <gnuradio-4.0/lora/tx_burst_taper.hpp>

const boost::ut::suite<"TX burst taper"> tx_burst_taper_tests = [] {
    using namespace boost::ut;
    using namespace gr::lora;
    using cf32 = std::complex<float>;

    "taper zero-guards head and tail for a full-length burst"_test = [] {
        constexpr std::size_t n = 4 * kTxTaperRampSamples;
        std::vector<cf32>     iq(n, cf32(1.f, 0.f));
        applyBurstTaper(iq);

        // rise[0] = 0 for RaisedCosine (u=0 -> (1-cos 0)/2 = 0)
        expect(lt(std::abs(iq.front()), 1e-6f)) << "iq[0] should be zero after rising ramp";
        // fall[last] = 0
        expect(lt(std::abs(iq.back()), 1e-6f)) << "iq[back] should be zero after falling ramp";

        // Interior samples (between ramps) are untouched — envelope = 1
        for (std::size_t i = kTxTaperRampSamples; i < n - kTxTaperRampSamples; ++i) {
            expect(eq(iq[i].real(), 1.f)) << "interior[" << i << "].real unchanged";
            expect(eq(iq[i].imag(), 0.f)) << "interior[" << i << "].imag unchanged";
        }
    };

    "taper rising ramp is monotonic non-decreasing"_test = [] {
        constexpr std::size_t n = 4 * kTxTaperRampSamples;
        std::vector<cf32>     iq(n, cf32(1.f, 0.f));
        applyBurstTaper(iq);
        for (std::size_t i = 1; i < kTxTaperRampSamples; ++i) {
            expect(ge(std::abs(iq[i]), std::abs(iq[i - 1]))) << "rise monotonic at i=" << i;
        }
    };

    "taper falling ramp mirrors rising ramp"_test = [] {
        constexpr std::size_t n = 4 * kTxTaperRampSamples;
        std::vector<cf32>     iq(n, cf32(1.f, 0.f));
        applyBurstTaper(iq);
        const std::size_t tail = n - kTxTaperRampSamples;
        for (std::size_t i = 0; i < kTxTaperRampSamples; ++i) {
            const float err = std::abs(std::abs(iq[i]) - std::abs(iq[tail + kTxTaperRampSamples - 1 - i]));
            expect(lt(err, 1e-5f)) << "rise/fall symmetric at i=" << i;
        }
    };

    "taper short-burst guard leaves input unchanged"_test = [] {
        std::vector<cf32> iq(2 * kTxTaperRampSamples - 1, cf32(0.5f, 0.25f));
        const auto        before = iq;
        applyBurstTaper(iq);
        expect(eq(iq.size(), before.size())) << "size unchanged";
        for (std::size_t i = 0; i < iq.size(); ++i) {
            expect(eq(iq[i].real(), before[i].real())) << "short-burst iq[" << i << "].real unchanged";
            expect(eq(iq[i].imag(), before[i].imag())) << "short-burst iq[" << i << "].imag unchanged";
        }
    };

    "prependHeadPadAndTaper keeps the first live sample at envelope = 1"_test = [] {
        constexpr std::size_t n = 4 * kTxTaperRampSamples;
        // Original burst: all unit magnitude, no leading silence.
        std::vector<cf32> iq(n, cf32(1.f, 0.f));
        prependHeadPadAndTaper(iq);
        // Size grows by kTxTaperRampSamples (head pad) then taper mutates.
        expect(eq(iq.size(), n + kTxTaperRampSamples)) << "size = original + head pad";
        // The first live sample (index kTxTaperRampSamples) has rising envelope
        // applied with u=1 → envelope = 1, so it's effectively untouched.
        expect(eq(iq[kTxTaperRampSamples].real(), 1.f)) << "first live sample untouched";
        // Prepended zeros stay ≈ 0 after rising ramp times zero = zero.
        for (std::size_t i = 0; i < kTxTaperRampSamples; ++i) {
            expect(lt(std::abs(iq[i]), 1e-6f)) << "prepended zero[" << i << "] stays silent";
        }
    };
};

int main() { /* boost::ut auto-runs all suites */ }
