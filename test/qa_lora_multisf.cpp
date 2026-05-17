// SPDX-License-Identifier: ISC
//
// Phase 8 unit tests: MultiSfDecoder graph-level loopback.
//
// Builds a trivial TagSource → MultiSfDecoder → TagSink graph, feeds a
// TX-chain-generated clean IQ stream in, and verifies the recovered payload,
// header info, and per-frame tags.

#include <boost/ut.hpp>

#include <cmath>
#include <complex>
#include <cstdint>
#include <numbers>
#include <string>
#include <vector>

#include <gnuradio-4.0/Graph.hpp>
#include <gnuradio-4.0/Scheduler.hpp>
#include <gnuradio-4.0/testing/TagMonitors.hpp>

#include <gnuradio-4.0/lora/MultiSfDecoder.hpp>
#include <gnuradio-4.0/lora/algorithm/HalfBandDecimator.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>

using namespace boost::ut;
using cf32 = std::complex<float>;

namespace {

struct LoopbackResult {
    std::vector<uint8_t> decoded;
    std::vector<gr::Tag> tags;
    bool                 ok = false;
};

[[nodiscard]] LoopbackResult run_loopback(const std::vector<uint8_t>& payload, uint8_t test_sf, uint8_t test_cr, uint32_t test_bw, uint8_t test_os, uint16_t sync_word = 0x12, uint16_t preamble_len = 8, uint8_t sf_min = 7, uint8_t sf_max = 12, double freq_offset_hz = 0.0) {
    using namespace gr;
    using namespace gr::lora;

    const uint32_t sps = (1u << test_sf) * test_os;

    auto iq = generate_frame_iq(payload, test_sf, test_cr, test_os, sync_word, preamble_len,
        /*has_crc=*/true,
        /*zero_pad=*/sps * 5,
        /*ldro_mode=*/2, test_bw,
        /*inverted_iq=*/false);

    // Apply a sub-channel carrier frequency offset (CFO) to the entire IQ
    // stream. The receiver sees a frame whose chirps are shifted by
    // freq_offset_hz from baseband, mimicking the effect of a wideband
    // slot whose NCO doesn't land exactly on the channel grid (the
    // wideband-loopback failure mode the FIXME documents).
    if (freq_offset_hz != 0.0) {
        const double sample_rate = static_cast<double>(test_bw) * test_os;
        const double phase_inc   = 2.0 * std::numbers::pi * freq_offset_hz / sample_rate;
        double       phase       = 0.0;
        for (auto& s : iq) {
            const cf32 rot{static_cast<float>(std::cos(phase)), static_cast<float>(std::sin(phase))};
            s *= rot;
            phase += phase_inc;
        }
    }

    Graph graph;
    auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(iq.size())}, {"repeat_tags", false}, {"mark_tag", false}});
    src.values = iq;

    auto& decoder        = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
    decoder.bandwidth    = test_bw;
    decoder.sync_word    = sync_word;
    decoder.os_factor    = test_os;
    decoder.preamble_len = preamble_len;
    // Build sf_set from the legacy sf_min/sf_max range.
    decoder.sf_set.clear();
    for (uint8_t s = sf_min; s <= sf_max; ++s) {
        decoder.sf_set.push_back(s);
    }
    decoder.p_false_alarm = 0.01;

    auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

    (void)graph.connect<"out", "in">(src, decoder);
    (void)graph.connect<"out", "in">(decoder, sink);

    scheduler::Simple sched;
    if (auto ret = sched.exchange(std::move(graph)); !ret) {
        return {};
    }
    sched.runAndWait();

    LoopbackResult result;
    result.decoded.assign(sink._samples.begin(), sink._samples.end());
    result.tags.assign(sink._tags.begin(), sink._tags.end());
    result.ok = true;
    return result;
}

[[nodiscard]] bool payload_matches(const std::vector<uint8_t>& expected, const std::vector<uint8_t>& decoded) {
    if (decoded.size() < expected.size()) {
        return false;
    }
    for (std::size_t i = 0; i < expected.size(); ++i) {
        if (decoded[i] != expected[i]) {
            return false;
        }
    }
    return true;
}

[[nodiscard]] bool tag_crc_valid(const std::vector<gr::Tag>& tags) {
    for (const auto& t : tags) {
        if (auto it = t.map.find("crc_valid"); it != t.map.end()) {
            return it->second.value_or<bool>(false);
        }
    }
    return false;
}

[[maybe_unused]] [[nodiscard]] int64_t tag_sf(const std::vector<gr::Tag>& tags) {
    for (const auto& t : tags) {
        if (auto it = t.map.find("sf"); it != t.map.end()) {
            return it->second.value_or<int64_t>(-1);
        }
    }
    return -1;
}

/// Loopback variant that captures every property_map the decoder publishes
/// via its `_telemetry` callback. The result holds the decoded payload, the
/// per-frame tags, AND the full ordered list of telemetry events. Used by
/// the telemetry suite to assert on multisf_sync / multisf_fail.
struct TelemetryLoopbackResult {
    std::vector<uint8_t>          decoded;
    std::vector<gr::Tag>          tags;
    std::vector<gr::property_map> events;
    bool                          ok = false;
};

[[nodiscard]] TelemetryLoopbackResult run_loopback_with_telemetry(const std::vector<uint8_t>& payload, uint8_t test_sf, uint8_t test_cr, uint32_t test_bw, uint8_t test_os, uint16_t sync_word = 0x12, uint16_t preamble_len = 8, uint8_t sf_min = 7, uint8_t sf_max = 12, double freq_offset_hz = 0.0) {
    using namespace gr;
    using namespace gr::lora;

    const uint32_t sps = (1u << test_sf) * test_os;

    auto iq = generate_frame_iq(payload, test_sf, test_cr, test_os, sync_word, preamble_len,
        /*has_crc=*/true,
        /*zero_pad=*/sps * 5,
        /*ldro_mode=*/2, test_bw,
        /*inverted_iq=*/false);

    // Match run_loopback: inject carrier frequency offset via sample-
    // domain phase ramp before feeding the decoder. Zero offset = clean
    // synthetic frame (existing telemetry test behaviour).
    if (freq_offset_hz != 0.0) {
        const double sample_rate = static_cast<double>(test_bw) * test_os;
        const double phase_inc   = 2.0 * std::numbers::pi * freq_offset_hz / sample_rate;
        double       phase       = 0.0;
        for (auto& s : iq) {
            const cf32 rot{static_cast<float>(std::cos(phase)), static_cast<float>(std::sin(phase))};
            s *= rot;
            phase += phase_inc;
        }
    }

    Graph graph;
    auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(iq.size())}, {"repeat_tags", false}, {"mark_tag", false}});
    src.values = iq;

    auto& decoder        = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
    decoder.bandwidth    = test_bw;
    decoder.sync_word    = sync_word;
    decoder.os_factor    = test_os;
    decoder.preamble_len = preamble_len;
    decoder.sf_set.clear();
    for (uint8_t s = sf_min; s <= sf_max; ++s) {
        decoder.sf_set.push_back(s);
    }
    decoder.p_false_alarm = 0.01;

    // Captured by-ref into the lambda, drained into the result after run.
    // No mutex needed: the GR4 Simple scheduler invokes processBulk on the
    // calling thread, so the telemetry callback fires from the same thread
    // that calls runAndWait().
    std::vector<gr::property_map> events;
    decoder._telemetry = [&events](const gr::property_map& evt) { events.push_back(evt); };

    auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

    (void)graph.connect<"out", "in">(src, decoder);
    (void)graph.connect<"out", "in">(decoder, sink);

    scheduler::Simple sched;
    if (auto ret = sched.exchange(std::move(graph)); !ret) {
        return {};
    }
    sched.runAndWait();

    TelemetryLoopbackResult result;
    result.decoded.assign(sink._samples.begin(), sink._samples.end());
    result.tags.assign(sink._tags.begin(), sink._tags.end());
    result.events = std::move(events);
    result.ok     = true;
    return result;
}

/// Find the first event with the given "type" string. Returns nullptr if
/// no such event was published. Used by telemetry suite assertions.
[[nodiscard]] const gr::property_map* find_event(const std::vector<gr::property_map>& events, std::string_view type_name) {
    for (const auto& evt : events) {
        auto it = evt.find("type");
        if (it == evt.end()) {
            continue;
        }
        const auto sv = it->second.value_or(std::string_view{});
        if (sv == type_name) {
            return &evt;
        }
    }
    return nullptr;
}

suite<"MultiSfDecoder loopback"> _loopback = [] {
    "SF7 CR1 BW125 os4"_test = [] {
        const std::vector<uint8_t> payload{'H', 'E', 'L', 'L', 'O'};
        auto                       r = run_loopback(payload, 7, 1, 125000, 4,
            /*sync_word=*/0x12, /*preamble=*/8,
            /*sf_min=*/7, /*sf_max=*/7);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    "SF8 CR4 BW125 os4"_test = [] {
        const std::vector<uint8_t> payload{'L', 'O', 'R', 'A'};
        auto                       r = run_loopback(payload, 8, 4, 125000, 4, 0x12, 8, 8, 8);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF9 CR2 BW125 os4"_test = [] {
        const std::vector<uint8_t> payload{0x01, 0x02, 0x03, 0x04, 0x05};
        auto                       r = run_loopback(payload, 9, 2, 125000, 4, 0x12, 8, 9, 9);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF10 CR1 BW125 os4"_test = [] {
        const std::vector<uint8_t> payload{'T', 'E', 'S', 'T'};
        auto                       r = run_loopback(payload, 10, 1, 125000, 4, 0x12, 8, 10, 10);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF8 CR4 BW62.5k os64 (4 MS/s IIO)"_test = [] {
        const std::vector<uint8_t> payload{'L', 'O', 'R', 'A'};
        auto                       r = run_loopback(payload, 8, 4, 62500, 64, 0x12, 16, 7, 12);
        expect(r.ok) << "decoder did not emit a frame";
        expect(payload_matches(payload, r.decoded)) << "decoded payload mismatch";
        expect(tag_crc_valid(r.tags)) << "CRC failed";
    };

    "SF7 CR4 BW62.5k os64 (4 MS/s IIO)"_test = [] {
        const std::vector<uint8_t> payload{'H', 'E', 'L', 'L', 'O'};
        auto                       r = run_loopback(payload, 7, 4, 62500, 64, 0x12, 16, 7, 12);
        expect(r.ok) << "decoder did not emit a frame";
        expect(payload_matches(payload, r.decoded)) << "decoded payload mismatch";
        expect(tag_crc_valid(r.tags)) << "CRC failed";
    };

    "SF7 longer payload 16 bytes"_test = [] {
        std::vector<uint8_t> payload(16);
        for (std::size_t i = 0; i < payload.size(); ++i) {
            payload[i] = static_cast<uint8_t>(0x40 + i);
        }
        auto r = run_loopback(payload, 7, 1, 125000, 4, 0x12, 8, 7, 7);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF7..SF12 lanes configured, SF8 frame"_test = [] {
        const std::vector<uint8_t> payload{'X', 'Y', 'Z'};
        // sf_min=7, sf_max=12 — all 6 lanes instantiated.
        auto r = run_loopback(payload, 8, 1, 125000, 4, 0x12, 8, 7, 12);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    // preamble_len regression guards for MeshCore interoperability.
    //
    // MeshCore firmware (verified 2026-04-09 in MeshCore
    // src/helpers/radiolib/CustomSX1262.h:45 and all MeshCore variant
    // target.cpp files) initialises the SX1262 via RadioLib
    // `radio.begin(..., preambleLength=16, ...)`. The 7th positional
    // parameter of SX1262::begin() is the LoRa preamble length in
    // symbols. Every MeshCore-compatible transmitter therefore sends
    // 16 upchirps before NID1/NID2/SFD, NOT the LoRa standard 8.
    //
    // The PreambleSync state machine relies on
    // `_extra_up_remaining = cfg.preamble_len - 7` in Stage 2.5 to
    // consume the upchirps between U6 and U_last (the final s_up
    // measurement). If cfg.preamble_len is wrong, Stage 3a/3b/3c read
    // pure upchirps instead of U_last/NID1/NID2 and sync word
    // verification rejects. The loopback harness must cover several
    // non-8 preamble lengths so a future refactor of the skip math is
    // caught before it ships.
    "SF8 preamble_len=16 (MeshCore default)"_test = [] {
        const std::vector<uint8_t> payload{'M', 'C', '1', '6'};
        auto                       r = run_loopback(payload, 8, 1, 62500, 4,
            /*sync_word=*/0x12, /*preamble=*/16,
            /*sf_min=*/8, /*sf_max=*/8);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    "SF7 preamble_len=16 (MeshCore default, SF7)"_test = [] {
        const std::vector<uint8_t> payload{'S', 'F', '7'};
        auto                       r = run_loopback(payload, 7, 1, 125000, 4, 0x12, 16, 7, 7);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF10 preamble_len=12"_test = [] {
        const std::vector<uint8_t> payload{'P', 'R', 'E', '1', '2'};
        auto                       r = run_loopback(payload, 10, 1, 125000, 4, 0x12, 12, 10, 10);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };

    "SF7..SF12 lanes, preamble_len=16 SF8 frame"_test = [] {
        const std::vector<uint8_t> payload{'A', 'L', 'L'};
        auto                       r = run_loopback(payload, 8, 1, 125000, 4, 0x12, 16, 7, 12);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));
        expect(tag_crc_valid(r.tags));
    };
};

// Sample-window-alignment regression suite. Guards the bug fix where
// PreambleSync correctly extracted integer STO via the up/down chirp
// formula (Xhonneux §5.2) and reported it on SyncResult, but neither
// MultiSfDecoder nor WidebandDecoder applied it to the post-SFD sample
// alignment. sfd_tail_remaining is now adjusted at the DETECT → Locked
// transition using the formula items_to_consume = N - k_hat.
//
// At os=4, the bug never triggered because the oversampled decimation
// gave enough timing headroom. At os=1 (the wideband ChannelSlot path)
// every sample was a Nyquist sample with zero headroom — and the cascade
// half-band decimator's group delay produced exactly the kind of integer
// STO this fix corrects.
suite<"MultiSfDecoder sample alignment"> _cfo = [] {
    "SF8 BW125 os1 with +3.3 kHz freq shift"_test = [] {
        const std::vector<uint8_t> payload{'C', 'F', 'O', '+'};
        auto                       r = run_loopback(payload, 8, 1, 125000, /*os=*/1, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/3300.0);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    "SF8 BW125 os1 with -3.3 kHz freq shift"_test = [] {
        const std::vector<uint8_t> payload{'C', 'F', 'O', '-'};
        auto                       r = run_loopback(payload, 8, 1, 125000, /*os=*/1, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/-3300.0);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    "SF7 BW125 os1 with +1.5 kHz freq shift"_test = [] {
        const std::vector<uint8_t> payload{'L', 'O', 'W'};
        auto                       r = run_loopback(payload, 7, 1, 125000, /*os=*/1, 0x12, 8, 7, 7,
            /*freq_offset_hz=*/1500.0);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    // Wideband residual test: -8 kHz at SF8/BW62.5k matches the actual
    // residual the wideband loopback test produces (signal at 869.500
    // MHz, channel center at 869.508 MHz).
    "SF8 BW62.5k os1 with -8 kHz freq shift (matches wb residual)"_test = [] {
        const std::vector<uint8_t> payload{0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02, 0x03, 0x04};
        auto                       r = run_loopback(payload, 8, 4, 62500, /*os=*/1, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/-8000.0);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    // Direct exercise of the cascade half-band interp + decim group
    // delay: the original FIXME failure mode. Generates a frame at os=1
    // narrowband, interpolates by 256 to wideband rate, decimates back
    // through the SAME CascadedDecimator the wideband slot uses, and
    // feeds the result to MultiSfDecoder at os=1. The decimator's
    // 8-stage cumulative group delay produces ~21 samples of integer
    // STO that the fix must compensate for.
    "SF8 BW62.5k os1 via 256x cascade interp+decim round trip"_test = [] {
        using namespace gr;
        using namespace gr::lora;

        constexpr uint8_t  sf      = 8;
        constexpr uint8_t  cr      = 1;
        constexpr uint32_t bw      = 62'500;
        constexpr double   wb_rate = 16e6;
        constexpr uint32_t cascade = static_cast<uint32_t>(wb_rate / bw); // 256

        const std::vector<uint8_t> payload{'B', 'I', 'S', 'E', 'C', 'T'};
        auto                       nb_iq = generate_frame_iq(payload, sf, cr, /*os_factor=*/1, 0x12, 8, true, (1u << sf) * 5,
            /*ldro_mode=*/2, bw, false);

        // Cascade interpolate by 256 (same as wideband test).
        uint32_t nStages = 0;
        for (uint32_t v = cascade; v > 1; v >>= 1) {
            ++nStages;
        }
        auto interp = [](const std::vector<cf32>& in) {
            using namespace gr::lora::halfband_detail;
            std::vector<cf32> stuffed(in.size() * 2);
            for (std::size_t i = 0; i < in.size(); ++i) {
                stuffed[i * 2]     = in[i] * 2.0f;
                stuffed[i * 2 + 1] = cf32{0.f, 0.f};
            }
            std::vector<cf32>            out(stuffed.size());
            std::array<cf32, kFilterLen> delay{};
            for (std::size_t n = 0; n < stuffed.size(); ++n) {
                for (std::size_t k = kFilterLen - 1; k > 0; --k) {
                    delay[k] = delay[k - 1];
                }
                delay[0] = stuffed[n];
                cf32 acc{0.f, 0.f};
                for (std::size_t c = 0; c < kCoeffs.size(); ++c) {
                    std::size_t k       = c * 2;
                    std::size_t kMirror = kFilterLen - 1 - k;
                    acc += kCoeffs[c] * (delay[k] + delay[kMirror]);
                }
                acc += kCenterTap * delay[kFilterLen / 2];
                out[n] = acc;
            }
            return out;
        };
        std::vector<cf32> wb = nb_iq;
        for (uint32_t s = 0; s < nStages; ++s) {
            wb = interp(wb);
        }

        // Now decimate it back through the SAME CascadedDecimator the slot
        // would use (no NCO shift, since signal is at baseband).
        gr::lora::CascadedDecimator decim;
        decim.init(nStages, /*maxChunkSize=*/65536);
        std::vector<cf32> nb_recovered;
        nb_recovered.reserve(nb_iq.size());
        cf32       nco_rot{1.f, 0.f};
        const cf32 nco_step{1.f, 0.f}; // identity NCO
        // Push in chunks of 8192 like the real graph
        for (std::size_t off = 0; off < wb.size(); off += 8192) {
            const std::size_t end = std::min(off + 8192, wb.size());
            decim.processWithNcoBatch(std::span<const cf32>(wb.data() + off, end - off), nb_recovered, nco_rot, nco_step);
        }

        // Feed nb_recovered directly to MultiSfDecoder at os=1.
        Graph graph;
        auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(nb_recovered.size())}, {"repeat_tags", false}, {"mark_tag", false}});
        src.values = nb_recovered;

        auto& decoder         = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
        decoder.bandwidth     = bw;
        decoder.sync_word     = 0x12;
        decoder.os_factor     = 1;
        decoder.preamble_len  = 8;
        decoder.sf_set        = {sf};
        decoder.p_false_alarm = 0.01;

        auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

        (void)graph.connect<"out", "in">(src, decoder);
        (void)graph.connect<"out", "in">(decoder, sink);

        scheduler::Simple sched;
        if (auto ret = sched.exchange(std::move(graph)); !ret) {
            expect(false) << "scheduler init failed";
            return;
        }
        sched.runAndWait();

        std::vector<uint8_t> decoded(sink._samples.begin(), sink._samples.end());
        expect(decoded.size() >= payload.size()) << "interp+decim+MultiSfDecoder os=1 decoded only " << decoded.size() << " bytes";
        expect(std::equal(payload.begin(), payload.end(), decoded.begin())) << "decoded payload mismatch";
    };

    // Integer-bin CFO at os=4, regression guards.
    //
    // These tests generate synthetic LoRa frames at os=4 with a
    // deliberate integer-bin carrier frequency offset, feed them to
    // MultiSfDecoder, and assert successful decode. Integer-bin CFO
    // is handled by CssDemod::set_cfo_correction (FFT-domain bin
    // remap from the PreambleSync-estimated cfo_int).
    //
    // Bin width at SF8/BW62.5k: bw/N = 62500/256 = 244.14 Hz/bin.
    // A +10-bin offset is +2441.4 Hz. A -10-bin offset is -2441.4 Hz.
    // These match the low end of the ±20..±200 cfo_int range seen
    // on narrowband hardware traces.
    "SF8 BW62.5k os4 with +10-bin cfo_int (+2441 Hz) [regression guard]"_test = [] {
        const std::vector<uint8_t> payload{'C', 'F', 'O', '4', '+'};
        constexpr double           kBinHz = 62500.0 / 256.0; // SF8
        auto                       r      = run_loopback(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/62500, /*os=*/4, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/10.0 * kBinHz);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    "SF8 BW62.5k os4 with -10-bin cfo_int (-2441 Hz) [regression guard]"_test = [] {
        const std::vector<uint8_t> payload{'C', 'F', 'O', '4', '-'};
        constexpr double           kBinHz = 62500.0 / 256.0; // SF8
        auto                       r      = run_loopback(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/62500, /*os=*/4, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/-10.0 * kBinHz);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    // Fractional-bin CFO. Pure fractional CFO is handled by
    // PreambleSync's Xhonneux §5.1 fractional CFO estimator feedback
    // into the CFO-corrected downchirp reference. Large fractional
    // CFO (e.g. 3.5 bins = 854 Hz at SF8/BW62.5k) is where an
    // FFT-only correction would prove insufficient.
    "SF8 BW62.5k os4 with +3.5-bin cfo (fractional, +854 Hz)"_test = [] {
        const std::vector<uint8_t> payload{'F', 'R', 'A', 'C', '+'};
        constexpr double           kBinHz = 62500.0 / 256.0; // SF8
        auto                       r      = run_loopback(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/62500, /*os=*/4, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/3.5 * kBinHz);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    // Large-CFO stress: 50 bins at SF8/BW62.5k = 12.2 kHz, close to
    // 20% of the bandwidth and matches the upper end of the ±20..±200
    // cfo_int range seen on narrowband hardware traces.
    "SF8 BW62.5k os4 with +50-bin cfo_int (+12.2 kHz, large)"_test = [] {
        const std::vector<uint8_t> payload{'B', 'I', 'G', '+'};
        constexpr double           kBinHz = 62500.0 / 256.0; // SF8
        auto                       r      = run_loopback(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/62500, /*os=*/4, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/50.0 * kBinHz);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded)) << "decoded size=" << r.decoded.size();
        expect(tag_crc_valid(r.tags));
    };

    // Sub-sample STO at os=4: injected by prepending a non-multiple-of-os
    // number of samples to the IQ buffer before feeding the decoder.
    // At os=4, prepending 1 sample shifts the decimation phase by 1/4
    // of a Nyquist-sample. MultiSfDecoder applies a sub-sample STO
    // correction that rounds `sto_frac * os_factor` to the nearest
    // integer and shifts the SFD tail sample window by that amount.
    //
    // Real preambles never start on an exact oversampled-sample
    // boundary relative to the receiver's frame grid — this was the
    // dominant narrowband failure mode before the correction landed.
    auto run_sto_prepend = [](std::size_t n_prepend, const std::vector<uint8_t>& payload) {
        using namespace gr;
        using namespace gr::lora;

        const uint8_t  sf  = 8;
        const uint8_t  cr  = 1;
        const uint32_t bw  = 62500;
        const uint8_t  os  = 4;
        const uint32_t sps = (1u << sf) * os;

        auto iq = generate_frame_iq(payload, sf, cr, os, 0x12, 8, true, sps * 5, 2, bw, false);

        std::vector<cf32> shifted;
        shifted.reserve(iq.size() + n_prepend);
        for (std::size_t i = 0; i < n_prepend; ++i) {
            shifted.emplace_back(0.0f, 0.0f);
        }
        shifted.insert(shifted.end(), iq.begin(), iq.end());

        Graph graph;
        auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(shifted.size())}, {"repeat_tags", false}, {"mark_tag", false}});
        src.values = shifted;

        auto& decoder         = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
        decoder.bandwidth     = bw;
        decoder.sync_word     = 0x12;
        decoder.os_factor     = os;
        decoder.preamble_len  = 8;
        decoder.sf_set        = {sf};
        decoder.p_false_alarm = 0.01;

        auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

        (void)graph.connect<"out", "in">(src, decoder);
        (void)graph.connect<"out", "in">(decoder, sink);

        scheduler::Simple sched;
        if (auto ret = sched.exchange(std::move(graph)); !ret) {
            return std::vector<uint8_t>{};
        }
        sched.runAndWait();

        return std::vector<uint8_t>(sink._samples.begin(), sink._samples.end());
    };

    "SF8 BW62.5k os4 with 1-sample prepend (sub-sample STO 0.25)"_test = [run_sto_prepend] {
        const std::vector<uint8_t> payload{'S', 'T', 'O', '1'};
        auto                       decoded = run_sto_prepend(1, payload);
        expect(decoded.size() >= payload.size()) << "1-sample prepend decoded " << decoded.size() << " bytes (expected " << payload.size() << ")";
        if (decoded.size() >= payload.size()) {
            expect(payload_matches(payload, decoded));
        }
    };

    "SF8 BW62.5k os4 with 2-sample prepend (sub-sample STO 0.5)"_test = [run_sto_prepend] {
        const std::vector<uint8_t> payload{'S', 'T', 'O', '2'};
        auto                       decoded = run_sto_prepend(2, payload);
        expect(decoded.size() >= payload.size()) << "2-sample prepend decoded " << decoded.size() << " bytes";
        if (decoded.size() >= payload.size()) {
            expect(payload_matches(payload, decoded));
        }
    };

    "SF8 BW62.5k os4 with 3-sample prepend (sub-sample STO 0.75)"_test = [run_sto_prepend] {
        const std::vector<uint8_t> payload{'S', 'T', 'O', '3'};
        auto                       decoded = run_sto_prepend(3, payload);
        expect(decoded.size() >= payload.size()) << "3-sample prepend decoded " << decoded.size() << " bytes";
        if (decoded.size() >= payload.size()) {
            expect(payload_matches(payload, decoded));
        }
    };

    // OS=40 STO prepend regression.
    // 10 oversampled zeros = 10/40 = 0.25 Nyquist sample offset.
    // Same sub-sample STO as the os4 prepend-1 test, but through 40x stride.
    auto run_sto_prepend_os40 = [](std::size_t n_prepend, const std::vector<uint8_t>& payload) {
        using namespace gr;
        using namespace gr::lora;
        const uint8_t  sf  = 8;
        const uint8_t  cr  = 4;
        const uint32_t bw  = 62500;
        const uint8_t  os  = 40;
        const uint32_t sps = (1u << sf) * os;
        auto iq = generate_frame_iq(payload, sf, cr, os, 0x12, 8, true, sps * 5, 2, bw, false);
        std::vector<cf32> shifted;
        shifted.reserve(iq.size() + n_prepend);
        for (std::size_t i = 0; i < n_prepend; ++i) shifted.emplace_back(0.f, 0.f);
        shifted.insert(shifted.end(), iq.begin(), iq.end());
        Graph graph;
        auto& src = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{{"n_samples_max", static_cast<gr::Size_t>(shifted.size())}, {"repeat_tags", false}}});
        src.values = shifted;
        auto& decoder = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
        decoder.bandwidth = bw; decoder.sync_word = 0x12; decoder.os_factor = os;
        decoder.preamble_len = 8; decoder.sf_set = {sf}; decoder.p_false_alarm = 0.01;
        auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{{"log_samples", true}}});
        (void)graph.connect<"out", "in">(src, decoder);
        (void)graph.connect<"out", "in">(decoder, sink);
        scheduler::Simple sched;
        if (auto ret = sched.exchange(std::move(graph)); !ret) return std::vector<uint8_t>{};
        sched.runAndWait();
        return std::vector<uint8_t>(sink._samples.begin(), sink._samples.end());
    };

    "SF8 BW62.5k os40 with 10-sample prepend (sub-sample STO 0.25)"_test = [run_sto_prepend_os40] {
        const std::vector<uint8_t> payload{'S', 'T', '4', '0'};
        auto decoded = run_sto_prepend_os40(10, payload);
        expect(decoded.size() >= payload.size()) << "os40 10-prepend decoded " << decoded.size();
        if (decoded.size() >= payload.size()) expect(payload_matches(payload, decoded));
    };

    "SF8 BW62.5k os40 with 20-sample prepend (sub-sample STO 0.50)"_test = [run_sto_prepend_os40] {
        const std::vector<uint8_t> payload{'S', 'T', '4', '1'};
        auto decoded = run_sto_prepend_os40(20, payload);
        expect(decoded.size() >= payload.size()) << "os40 20-prepend decoded " << decoded.size();
        if (decoded.size() >= payload.size()) expect(payload_matches(payload, decoded));
    };
};

// Telemetry instrumentation suite. Asserts on the property_maps that
// MultiSfDecoder publishes via the _telemetry callback. The wire path is:
//
//   processLaneSymbol() → _telemetry(evt) → telemetry::encode() → UDP CBOR
//
// Block-internal _telemetry is the only insertion point we control from
// unit tests; the lora_trx UDP path is exercised by hardware A/B runs.
//
// These tests exist so that downstream debug tooling (lora_test.py harness,
// trx_perf.py monitor) has a stable contract on the event schemas, and so
// any future change that drops a field is caught by ctest before it ships.
suite<"MultiSfDecoder telemetry"> _telemetry = [] {
    "multisf_sync fires on every successful PreambleSync lock"_test = [] {
        const std::vector<uint8_t> payload{'S', 'Y', 'N', 'C'};
        auto                       r = run_loopback_with_telemetry(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/125000, /*os=*/4,
            /*sync_word=*/0x12,
            /*preamble=*/8,
            /*sf_min=*/8, /*sf_max=*/8);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));

        const auto* evt = find_event(r.events, "multisf_sync");
        expect(evt != nullptr) << "multisf_sync event was never published";
        if (evt == nullptr) {
            return;
        }

        // Schema: every field that hardware debugging needs at sync time.
        expect(evt->contains("sf"));
        expect(evt->contains("bw"));
        expect(evt->contains("os_factor"));
        expect(evt->contains("cfo_int"));
        expect(evt->contains("cfo_frac"));
        expect(evt->contains("sto_int"));
        expect(evt->contains("snr_db"));
        expect(evt->contains("tail"));
        expect(evt->contains("nominal_tail"));

        // Per-field type sanity (catches encoder bugs that pass cbor_dump
        // but break Python consumers).
        if (auto it = evt->find("sf"); it != evt->end()) {
            expect(it->second.value_or<uint32_t>(0u) == 8u) << "sf should equal lane SF";
        }
        if (auto it = evt->find("bw"); it != evt->end()) {
            expect(it->second.value_or<uint32_t>(0u) == 125000u) << "bw should equal decoder bandwidth";
        }
        if (auto it = evt->find("os_factor"); it != evt->end()) {
            expect(it->second.value_or<uint32_t>(0u) == 4u) << "os_factor should equal decoder os_factor";
        }
    };

    "multisf_sync reports os_factor=1 for cascade-decim path"_test = [] {
        // The os==1 path is the wideband loopback round-trip. Even though
        // it's covered by qa_lora_wideband end-to-end, we want a small
        // direct exercise here so the telemetry contract is verified at
        // both os values.
        const std::vector<uint8_t> payload{'O', 'S', '1'};
        auto                       r = run_loopback_with_telemetry(payload, 7, 1, 125000, 1, 0x12, 8, 7, 7);
        expect(r.ok);
        expect(payload_matches(payload, r.decoded));

        const auto* evt = find_event(r.events, "multisf_sync");
        expect(evt != nullptr) << "multisf_sync missing at os=1";
        if (evt == nullptr) {
            return;
        }
        if (auto it = evt->find("os_factor"); it != evt->end()) {
            expect(it->second.value_or<uint32_t>(0u) == 1u);
        }
    };

    // Diagnostic: assert on the exact cfo_int / cfo_frac PreambleSync
    // reports at os=4 with a deliberate integer-bin frequency offset.
    // A +10-bin offset at SF8/BW62.5k (bin width 244.14 Hz) is
    // +2441.4 Hz. PreambleSync should decompose this as
    // cfo_int = 10, cfo_frac ≈ 0. If instead it reports cfo_int = 0
    // and cfo_frac ≠ 0, our "integer-bin CFO at os=4" test is actually
    // exercising the fractional-CFO path and the plan's candidate #1
    // hypothesis (missing sample-domain rotation of the integer part)
    // isn't being tested at all.
    // Sync word verification: PreambleSync rejects locks when the
    // decoded NID1/NID2 bins don't match the configured sync_word.
    // Without this check, 3 consecutive matching upchirps would count
    // as a lock — including noise-correlation tracks and cross-SF
    // spurs, which on ambient narrowband RX at os=4 produce 18-27
    // false locks/min with 0 decodes (2026-04-09 telemetry).
    //
    // Test: generate a frame with sync_word=0x34, configure the decoder
    // for sync_word=0x12, assert no multisf_sync event is emitted
    // (lock rejected) AND no frame decoded. Expected to FAIL until the
    // sync word verification is added.
    "multisf_sync NOT emitted for mismatched sync word (0x34 vs 0x12)"_test = [] {
        const std::vector<uint8_t> payload{'S', 'W', 'X'};
        // Generate with sync_word=0x34, decode with sync_word=0x12.
        using namespace gr;
        using namespace gr::lora;

        const uint8_t  sf  = 8;
        const uint8_t  cr  = 1;
        const uint32_t bw  = 125000;
        const uint8_t  os  = 4;
        const uint32_t sps = (1u << sf) * os;

        auto iq = generate_frame_iq(payload, sf, cr, os,
            /*sync_word=*/0x34, // WRONG sync word
            /*preamble_len=*/8, true, sps * 5, 2, bw, false);

        Graph graph;
        auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(iq.size())}, {"repeat_tags", false}, {"mark_tag", false}});
        src.values = iq;

        auto& decoder         = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
        decoder.bandwidth     = bw;
        decoder.sync_word     = 0x12; // EXPECTED sync word
        decoder.os_factor     = os;
        decoder.preamble_len  = 8;
        decoder.sf_set        = {sf};
        decoder.p_false_alarm = 0.01;

        std::vector<gr::property_map> events;
        decoder._telemetry = [&events](const gr::property_map& evt) { events.push_back(evt); };

        auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

        (void)graph.connect<"out", "in">(src, decoder);
        (void)graph.connect<"out", "in">(decoder, sink);

        scheduler::Simple sched;
        if (auto ret = sched.exchange(std::move(graph)); !ret) {
            expect(false) << "scheduler init failed";
            return;
        }
        sched.runAndWait();

        // No lock should have been emitted (no multisf_sync event).
        const auto* sync_evt = find_event(events, "multisf_sync");
        expect(sync_evt == nullptr) << "multisf_sync emitted for mismatched sync word — "
                                       "PreambleSync accepted a lock it should have rejected";

        // No payload decoded.
        std::vector<uint8_t> decoded(sink._samples.begin(), sink._samples.end());
        expect(decoded.empty()) << "decoded " << decoded.size() << " bytes for mismatched sync word"
                                << " — PreambleSync should have failed the lock";
    };

    // Regression guard: off-by-one in Stage 1 detection chain when
    // preceded by any Vangelista-passing non-matching tick. On real
    // hardware (2026-04-09 ambient MeshCore capture at SF8/BW62.5k
    // os=4), this bug produced a systematic 4-bin cfo_int bias
    // (measured cfo_int=12 vs. expected cfo_int=8) and 0% decodes.
    //
    // Root cause: resetDetectChain() previously set _detect_count=1
    // instead of 0. Inside runDetect the reset path falls through to
    // `_detect_count++`, so after a mismatch the chain actually held
    // 2 (1+1), not 1. That meant Stage 1 fired after only 2 matching
    // upchirps post-reset (not 3). With the reset at tick K, real
    // upchirps K+1 and K+2 were enough to fire — so Stage 3a read
    // u7 (not u8), Stage 3c read real NID1 (not NID2), Stage 3d read
    // real NID2 (not D1). The resulting cfo_int / sto_int combination
    // landed away from expected values and sync word verification
    // consistently rejected the lock.
    //
    // Test: prepend a frequency-shifted upchirp (build_upchirp id=100) ahead
    // of a clean cfo=0 preamble. The prefix passes Vangelista but dechirps
    // to bin 100 — far enough from the preamble's argmax (bin 0) to
    // trigger resetDetectChain at tick 1. A correctly-implemented Stage 1
    // must consume exactly 3 subsequent preamble upchirps before firing,
    // land Stage 3a on u8, and produce cfo_int=0 with full payload decode.
    "Stage 1 fires on 3 consecutive upchirps even after leading non-matching tick"_test = [] {
        using namespace gr;
        using namespace gr::lora;

        constexpr uint8_t  sf  = 8;
        constexpr uint8_t  cr  = 1;
        constexpr uint32_t bw  = 62500;
        constexpr uint8_t  os  = 4;
        constexpr uint32_t sps = (1u << sf) * os;

        // Generate a clean frame at cfo=0 (no offset).
        const std::vector<uint8_t> payload{'O', 'F', 'F', '1'};
        auto                       clean = generate_frame_iq(payload, sf, cr, os, /*sync_word=*/0x12, /*preamble_len=*/8,
            /*has_crc=*/true, /*zero_pad=*/0, /*ldro_mode=*/2, bw,
            /*inverted_iq=*/false);

        // Prepend ONE full symbol (sps samples) of a non-matching "noise"
        // tick that will pass Vangelista. Use a frequency-shifted upchirp
        // (via build_upchirp with id=100) so its dechirp-argmax is
        // deterministically at bin 100, distant from the preamble's argmax
        // (bin 0) by enough to trigger the bin-mismatch resetDetectChain.
        std::vector<cf32> prefix(sps);
        build_upchirp(prefix.data(), /*id=*/100u, sf, os);

        std::vector<cf32> iq;
        iq.reserve(prefix.size() + clean.size());
        iq.insert(iq.end(), prefix.begin(), prefix.end());
        iq.insert(iq.end(), clean.begin(), clean.end());

        Graph graph;
        auto& src  = graph.emplaceBlock<testing::TagSource<cf32, testing::ProcessFunction::USE_PROCESS_BULK>>({{"n_samples_max", static_cast<gr::Size_t>(iq.size())}, {"repeat_tags", false}, {"mark_tag", false}});
        src.values = iq;

        auto& decoder         = graph.emplaceBlock<gr::lora::MultiSfDecoder>();
        decoder.bandwidth     = bw;
        decoder.sync_word     = 0x12;
        decoder.os_factor     = os;
        decoder.preamble_len  = 8;
        decoder.sf_set        = {sf};
        decoder.p_false_alarm = 0.01;

        std::vector<gr::property_map> events;
        decoder._telemetry = [&events](const gr::property_map& evt) { events.push_back(evt); };

        auto& sink = graph.emplaceBlock<testing::TagSink<uint8_t, testing::ProcessFunction::USE_PROCESS_BULK>>({{"log_samples", true}, {"log_tags", true}});

        (void)graph.connect<"out", "in">(src, decoder);
        (void)graph.connect<"out", "in">(decoder, sink);

        scheduler::Simple sched;
        if (auto ret = sched.exchange(std::move(graph)); !ret) {
            expect(false) << "scheduler init failed";
            return;
        }
        sched.runAndWait();

        // If Stage 1 correctly consumes 3 upchirps after a noise prefix,
        // the subsequent stages align with u8/NID1/NID2/D1 and the decoder
        // should produce cfo_int=0 and decode the full payload.
        const auto* sync_evt = find_event(events, "multisf_sync");
        expect(sync_evt != nullptr) << "no multisf_sync event — Stage 1 failed to lock after "
                                       "noise prefix (or sync word rejected due to misalignment)";

        if (sync_evt != nullptr) {
            int32_t reported_cfo_int = 0;
            if (auto it = sync_evt->find("cfo_int"); it != sync_evt->end()) {
                reported_cfo_int = it->second.value_or<int32_t>(0);
            }
            expect(reported_cfo_int == 0) << "PreambleSync reported cfo_int=" << reported_cfo_int << " — expected 0 for clean cfo=0 frame with noise prefix";
        }

        std::vector<uint8_t> decoded(sink._samples.begin(), sink._samples.end());
        expect(payload_matches(payload, decoded)) << "decoded " << decoded.size() << " bytes for noise-prefixed frame"
                                                  << " — expected " << payload.size();
    };

    "multisf_sync cfo_int reports +10 for +2441 Hz at SF8 os=4"_test = [] {
        const std::vector<uint8_t> payload{'D', 'I', 'A', 'G'};
        constexpr double           kBinHz = 62500.0 / 256.0;
        auto                       r      = run_loopback_with_telemetry(payload, /*sf=*/8, /*cr=*/1,
            /*bw=*/62500, /*os=*/4, 0x12, 8, 8, 8,
            /*freq_offset_hz=*/10.0 * kBinHz);
        expect(r.ok);

        const auto* evt = find_event(r.events, "multisf_sync");
        expect(evt != nullptr) << "multisf_sync event missing";
        if (evt == nullptr) {
            return;
        }

        int32_t reported_cfo_int  = 0;
        double  reported_cfo_frac = 0.0;
        if (auto it = evt->find("cfo_int"); it != evt->end()) {
            reported_cfo_int = it->second.value_or<int32_t>(0);
        }
        if (auto it = evt->find("cfo_frac"); it != evt->end()) {
            reported_cfo_frac = it->second.value_or<double>(0.0);
        }
        expect(reported_cfo_int == 10) << "PreambleSync reported cfo_int=" << reported_cfo_int << " cfo_frac=" << reported_cfo_frac << " — expected cfo_int=10, cfo_frac≈0 for a +10-bin offset";
        expect(std::abs(reported_cfo_frac) < 0.5) << "cfo_frac=" << reported_cfo_frac << " — expected |cfo_frac| < 0.5";
    };
};

} // namespace

int main() { return 0; }
