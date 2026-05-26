// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_MULTI_SF_DECODER_HPP
#define GNURADIO_LORA_MULTI_SF_DECODER_HPP

// gr::lora::MultiSfDecoder
//
// GR4 block wrapper for the LoRa PHY library. Drives one PerSfDecoder per
// configured SF in lockstep over a single narrowband IQ stream. Publishes
// per-frame tags and messages that FrameSink, the UDP emitter, and the
// downstream FlowGraph connectors consume.
//
// Pipeline per lane per symbol:
//   oversampled accum -> decimate to Nyquist -> PreambleSync.tick()
//     (Detect/Sync phases)
//   after Lock: skip SFD tail (D2 + quarter) -> CssDemod.demodHard()
//     -> DecodeChain.push_symbol() -> FrameResult on completion
//
// Scheduler rules (gr4-dev):
//   - gr::NoTagPropagation (custom tags would drop otherwise)
//   - never consume(0); always consume the full input, per-lane accum
//   - never throw from processBulk
//   - all settings in GR_MAKE_REFLECTABLE

#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <memory>
#include <span>
#include <string>
#include <vector>

#include <gnuradio-4.0/Block.hpp>
#include <gnuradio-4.0/BlockRegistry.hpp>
#include <gnuradio-4.0/Message.hpp>

#include <gnuradio-4.0/lora/algorithm/HalfBandDecimator.hpp>
#include <gnuradio-4.0/lora/algorithm/SpectrumTap.hpp>
#include <gnuradio-4.0/lora/log.hpp>
#include <gnuradio-4.0/lora/phy/AntennaCombiner.hpp>
#include <gnuradio-4.0/lora/phy/CssDemod.hpp>
#include <gnuradio-4.0/lora/phy/DecodeChain.hpp>
#include <gnuradio-4.0/lora/phy/PreambleSync.hpp>
#include <gnuradio-4.0/lora/phy/Types.hpp>

namespace gr::lora {

using cf32 = gr::lora::phy::cf32;

/// Per-spreading-factor decoder bundle. Not a GR4 block — this is the
/// per-lane state object owned by MultiSfDecoder.
struct PerSfDecoder {
    enum class State {
        Detect,      ///< PreambleSync in DetectU1U3/EstimateU4U6/Integer*
        SkipSfdTail, ///< Locked, skipping D2 + quarter downchirp samples
        Output,      ///< Feeding payload symbols to DecodeChain
    };

    uint8_t  sf        = 7;
    uint32_t N         = 0; ///< 2^sf
    uint8_t  os_factor = 4;
    uint32_t sps       = 0; ///< N × os_factor

    phy::PreambleSync    sync;
    phy::CssDemod        demod;
    phy::DecodeChain     decode;
    phy::AntennaCombiner combiner; ///< N=1 pass-through (scaffold)

    std::vector<cf32> accum;       ///< unprocessed samples at oversampled rate
    std::vector<cf32> nyquist_buf; ///< N samples, decimated per symbol

    // Optional anti-aliased decimator: cascaded half-band FIR (−45 dB stopband)
    // driven one oversampled symbol at a time. Reuses the same filter bank
    // the scan path's channelizer uses. Stateful across symbols — delay
    // lines carry history forward so the filter is correct over contiguous
    // streams. Reset in resetLane() so a re-acquisition starts clean.
    CascadedDecimator aa_decim{};
    std::vector<cf32> aa_out; ///< scratch output of processBatch
    bool              aa_enabled = false;

    State    state              = State::Detect;
    uint32_t sfd_tail_remaining   = 0; ///< samples left to skip after Lock
    uint32_t stall_symbols        = 0; ///< OUTPUT-stage stall counter
    uint32_t decim_phase          = 0; ///< decimation phase offset (0..os-1) after Lock
    uint32_t output_symbol_count  = 0; ///< symbols pushed in current Output run (for header diag)
    float    cached_snr_db      = 0.f;
    float    cached_noise_db    = -999.f;                            ///< noise floor from PreambleSync
    float    cached_peak_db     = -999.f;                            ///< peak power from PreambleSync
    int32_t  cached_cfo_int     = 0;                                 ///< integer CFO bins
    float    cached_cfo_frac    = 0.f;                               ///< fractional CFO
    float    cached_sfo_hat     = 0.f;                               ///< SFO estimate (derived from CFO)
    uint32_t cached_sync_word   = phy::SyncResult::kSyncWordUnknown; ///< promiscuous only

    void init(uint8_t sf_, uint32_t bandwidth, uint8_t os, uint16_t preamble, uint16_t sync_word, double p_false_alarm, bool promiscuous = false, bool use_aa_filter = false, bool decode_debug = false, bool implicit_header_ = false, uint8_t implicit_cr_ = 1, uint16_t implicit_pay_len_ = 28) {
        sf        = sf_;
        N         = uint32_t{1} << sf_;
        os_factor = os;
        sps       = N * os;

        phy::PreambleSync::Config sc;
        sc.sf               = sf_;
        sc.bandwidth_hz     = bandwidth;
        sc.preamble_len     = preamble;
        sc.sync_word        = sync_word;
        sc.p_false_alarm    = p_false_alarm;
        sc.verify_threshold = false; // DISABLE Vangelista alpha gate — accept weak AA filter detections
        sc.promiscuous      = promiscuous;
        sync.init(sc);

        phy::CssDemod::Config dc;
        dc.sf           = sf_;
        dc.bandwidth_hz = bandwidth;
        dc.soft_decode  = false;
        demod.init(dc);

        phy::DecodeChain::Config ec;
        ec.sf              = sf_;
        ec.bandwidth_hz    = bandwidth;
        ec.soft_decode     = false;
        ec.debug           = decode_debug;
        ec.implicit_header = implicit_header_;
        ec.default_cr      = implicit_cr_;
        ec.default_pay_len = implicit_pay_len_;
        decode.init(ec);

        phy::AntennaCombiner::Config ac;
        ac.sf           = sf_;
        ac.bandwidth_hz = bandwidth;
        ac.n_antennas   = 1;
        combiner.init(ac);

        accum.clear();
        nyquist_buf.assign(N, cf32{0.f, 0.f});

        // Set up the optional half-band anti-alias filter. nStages = log2(os)
        // is integer for os = 1, 2, 4, 8, 16; other values fall back to
        // stride decimation (assert instead of silent misbehaviour).
        aa_enabled = false;
        if (use_aa_filter && os > 1) {
            const auto nStagesF = std::log2(static_cast<double>(os));
            const auto nStages  = static_cast<std::size_t>(nStagesF);
            assert(std::abs(nStagesF - static_cast<double>(nStages)) < 1e-9 && "use_aa_filter requires os = power of two");
            aa_decim.init(nStages, sps);
            aa_out.reserve(N);
            aa_enabled = true;
        }

        resetLane();
    }

    void resetLane() noexcept {
        sync.reset();
        demod.reset_cfo_correction();
        decode.reset();
        state              = State::Detect;
        sfd_tail_remaining = 0;
        stall_symbols        = 0;
        decim_phase          = 0;
        output_symbol_count  = 0;
        cached_snr_db      = 0.f;
        cached_noise_db    = -999.f;
        cached_peak_db     = -999.f;
        cached_cfo_int     = 0;
        cached_cfo_frac    = 0.f;
        cached_sfo_hat     = 0.f;
        cached_sync_word   = phy::SyncResult::kSyncWordUnknown;
        // Flush FIR delay lines so a new acquisition doesn't see stale taps
        // from the previous burst (half-band stages are linear but carry
        // ~11 samples of history per stage).
        if (aa_enabled) {
            aa_decim.reset();
        }
    }
};

struct MultiSfDecoder : public gr::Block<MultiSfDecoder, gr::NoTagPropagation> {

    gr::PortIn<cf32>     in;
    gr::PortOut<uint8_t> out;
    gr::MsgPortOut       msg_out;

    // --- Settings ---
    uint32_t bandwidth    = 125000;
    uint16_t sync_word         = 0x12;  ///< expected sync word (ignored when promiscuous)
    bool     promiscuous       = false; ///< skip sync_word verify; surface observed in output tag
    uint8_t  os_factor         = 4;
    uint16_t preamble_len      = 8;
    uint32_t center_freq       = 869618000;
    int32_t  rx_channel        = -1;
    bool     implicit_header   = false; ///< implicit header mode for SF8+ on some networks
    uint8_t  implicit_cr       = 1;     ///< coding rate used when implicit_header=true
    uint16_t implicit_pay_len  = 28;    ///< payload length used when implicit_header=true
    /// Explicit SF subset to decode.  Empty = sweep SF7-12.  Allows
    /// non-contiguous subsets (e.g. [8, 11]).  Each entry must be in [7..12].
    std::vector<uint8_t> sf_set{};
    float                min_snr_db    = -19.0f;
    uint32_t             max_symbols   = 600;
    bool                 soft_decode   = false;
    double               p_false_alarm = 0.01;
    bool                 debug         = false;
    /// Use a cascaded half-band FIR before decimation (anti-alias), instead
    /// of plain stride decimation. Recommended when the input carries
    /// significant out-of-channel energy relative to the target bandwidth
    /// (e.g. SoapyPluto at 1 MS/s → BW=62.5k gives os=16; without a filter
    /// ~12 dB of adjacent-channel noise folds into baseband). Opt-in
    /// because the FIR cascade trades CPU for SNR and existing hardware
    /// baselines were recorded with stride decimation. When false, the
    /// decoder keeps the old behaviour exactly.
    bool use_aa_filter = false;

    GR_MAKE_REFLECTABLE(MultiSfDecoder, in, out, msg_out, bandwidth, sync_word, promiscuous, os_factor, preamble_len, center_freq, rx_channel, sf_set, min_snr_db, max_symbols, soft_decode, p_false_alarm, debug, use_aa_filter, implicit_header, implicit_cr, implicit_pay_len);

    std::vector<PerSfDecoder>                    _lanes;
    uint32_t                                     _min_sps      = 0;
    std::atomic<bool>*                           _channel_busy = nullptr; ///< LBT flag, optional
    std::function<void(const gr::property_map&)> _telemetry;
    std::shared_ptr<SpectrumState>               _spectrum_state;

    // Upstream timing-tag cache (from SoapyDevSource ppm_estimator).
    // Re-emitted on each frame so FrameSink can record the exact sample
    // rate / ppm-corrected frequency the frame was sampled at.
    // Task 7 (FrameSink) consumes these via the same tag keys.
    float  _last_sample_rate{0.f};
    double _last_frequency{0.0};
    float  _last_ppm{0.f};

    static constexpr uint32_t kMaxStallSymbols = 1000;

    /// Set external listen-before-talk channel-busy flag.
    void set_channel_busy_flag(std::atomic<bool>* flag) noexcept { _channel_busy = flag; }

    /// Snapshot of the last three timing tags observed on the input.
    /// Consumed by qa_lora_tag_forwarding (Task 8). Test-only.
    struct TimingTagCache {
        float  sample_rate;
        double frequency;
        float  ppm_error;
    };

    [[nodiscard]] TimingTagCache lastTimingTagsForTest() const noexcept { return {_last_sample_rate, _last_frequency, _last_ppm}; }

    void start() { reconfigure(true); }

    void settingsChanged(const gr::property_map& /*oldSettings*/, const gr::property_map& newSettings) {
        static constexpr std::array kPhyKeys   = {"sf_set", "bandwidth", "os_factor", "preamble_len", "sync_word", "promiscuous", "p_false_alarm"};
        const bool                  needRecalc = std::ranges::any_of(kPhyKeys, [&](const char* k) { return newSettings.contains(k); });
        if (needRecalc && !_lanes.empty()) {
            reconfigure(false);
        }
    }

    void reconfigure(bool isStartup = false) {
        assert(bandwidth > 0 && os_factor >= 1 && preamble_len >= 5);

        // Effective SF list: sf_set if non-empty, else full sweep [7..12].
        std::vector<uint8_t> effective_sfs = sf_set;
        if (effective_sfs.empty()) {
            effective_sfs = {7, 8, 9, 10, 11, 12};
        }
        for (uint8_t s : effective_sfs) {
            (void)s;
            assert(s >= 7 && s <= 12);
        }

        _lanes.clear();
        _lanes.reserve(effective_sfs.size());
        for (uint8_t s : effective_sfs) {
            _lanes.emplace_back();
            _lanes.back().init(s, bandwidth, os_factor, preamble_len, sync_word, p_false_alarm, promiscuous, use_aa_filter, debug, implicit_header, implicit_cr, implicit_pay_len);
        }
        // _min_sps = sps of the smallest SF in the set (drives gating).
        _min_sps            = std::numeric_limits<uint32_t>::max();
        uint8_t max_sf_used = 7;
        for (const auto& lane : _lanes) {
            _min_sps    = std::min(_min_sps, lane.sps);
            max_sf_used = std::max(max_sf_used, lane.sf);
        }
        if (_lanes.empty()) {
            _min_sps = 0; // defensive
        }

        if (isStartup) {
            // Gate the scheduler on the SMALLEST SF's sps. Each lane has its
            // own accumulator (lane.accum) that buffers samples across calls
            // until it has lane.sps for one symbol — so a small in.min_samples
            // is correct: it just means SF12 fills over multiple processBulk
            // calls before producing a symbol.
            //
            // Earlier this gate was `worst_sps = (1<<max_sf) * os_factor`. At
            // os=64 (4 MS/s / 62.5 kHz BW) and SF12 cap that's 262144 samples,
            // which empirically wedges the GR4 scheduler even when the
            // upstream Edge buffer was sized to 2× that — the reader gate
            // never unlocks.  Smallest-sps gate is buffer-size-independent.
            (void)max_sf_used;
            in.min_samples = static_cast<std::size_t>(_min_sps);
        }
    }

    [[nodiscard]] gr::work::Status processBulk(gr::InputSpanLike auto& input, gr::OutputSpanLike auto& output) noexcept {
        auto in_span = std::span(input);
        // Do NOT materialise `output` via `std::span(output)` — the
        // OutputSpan is threaded down to emitFrame so per-frame tags go
        // into the pre-reserved OutputSpan::tags buffer.  Upstream PR
        // #439/#625 turned Writer into SingleWriter with one-reserve-per-
        // lifetime semantics; OutputSpan's ctor already holds a tag
        // reservation, so `Block::publishTag` / `Port::publishTag` would
        // trip the CircularBuffer::SingleWriter double-reserve abort.

        // Cache per-chunk timing tags from SoapySource::ppm_estimator.
        // Selective re-emit in emitFrame preserves NoTagPropagation
        // (see gr4-dev/references/gr4_block_rules.md §"Custom tags vanish").
        // Upstream PR #625 multi-tag API: iterate input.tags() instead of
        // merging via mergedInputTag().  Multiple tags per chunk are
        // collapsed to the last-seen value of each key — matches the old
        // merged-tag semantics for these three scalar keys.
        for (const auto& [relIndex, tagMapRef] : input.tags()) {
            const auto& tagMap = tagMapRef.get(); // reference_wrapper -> map
            if (auto it = tagMap.find("sample_rate"); it != tagMap.end()) {
                _last_sample_rate = it->second.template value_or<float>(0.f);
            }
            if (auto it = tagMap.find("frequency"); it != tagMap.end()) {
                _last_frequency = it->second.template value_or<double>(0.0);
            }
            if (auto it = tagMap.find("ppm_error"); it != tagMap.end()) {
                _last_ppm = it->second.template value_or<float>(0.f);
            }
        }

        if (_lanes.empty()) {
            reconfigure();
        }

        if (in_span.size() < _min_sps) {
            std::ignore = input.consume(0UZ);
            output.publish(0UZ);
            return gr::work::Status::OK;
        }

        const std::size_t n_in    = in_span.size();
        std::size_t       out_idx = 0;

        // feed waterfall (limited to one symbol's worth to avoid stalling)
        if (_spectrum_state) {
            _spectrum_state->push(in_span.data(), std::min(n_in, static_cast<std::size_t>(_min_sps)));
        }

        for (auto& lane : _lanes) {
            // Append to per-lane accumulator (variable rate, per gr4-dev rule).
            lane.accum.insert(lane.accum.end(), in_span.begin(), in_span.end());

            auto        accum_span = std::span<const cf32>(lane.accum);
            std::size_t pos        = 0;
            while (pos + lane.sps <= accum_span.size()) {
                const std::size_t consumed = processLaneSymbol(lane, accum_span, pos, output, out_idx);
                if (consumed == 0) {
                    // Defensive: never infinite-loop.
                    pos += lane.sps;
                } else {
                    pos += consumed;
                }
            }
            if (pos > 0) {
                lane.accum.erase(lane.accum.begin(), lane.accum.begin() + static_cast<std::ptrdiff_t>(pos));
            }
        }

        // LBT channel-busy flag: any lane in Sync/SkipSfdTail/Output is "busy".
        if (_channel_busy != nullptr) {
            bool busy = false;
            for (const auto& lane : _lanes) {
                if (lane.state != PerSfDecoder::State::Detect) {
                    busy = true;
                    break;
                }
            }
            _channel_busy->store(busy, std::memory_order_relaxed);
        }

        std::ignore = input.consume(n_in);
        output.publish(out_idx);
        return gr::work::Status::OK;
    }

private:
    /// Process one symbol period (sps samples) for a single lane.
    /// Returns the number of oversampled samples consumed (normally sps,
    /// but the SkipSfdTail state consumes sps/4 for the quarter downchirp).
    /// `out_span` is the OutputSpan (NOT std::span<uint8_t>) so per-frame
    /// tags can be published into its pre-reserved `tags` buffer — see
    /// processBulk for the rationale.
    std::size_t processLaneSymbol(PerSfDecoder& lane, std::span<const cf32> in_span, std::size_t in_offset, auto& out_span, std::size_t& out_idx) {
        // SkipSfdTail: don't decimate, just skip raw samples.
        if (lane.state == PerSfDecoder::State::SkipSfdTail) {
            const std::size_t skip = std::min<std::size_t>(lane.sfd_tail_remaining, lane.sps);
            lane.sfd_tail_remaining -= static_cast<uint32_t>(skip);
            if (lane.sfd_tail_remaining == 0) {
                lane.state = PerSfDecoder::State::Output;
            }
            return skip == 0 ? lane.sps : skip;
        }

        // Decimate oversampled → Nyquist. Phase-centered within the
        // oversampling period (= os/2).
        decimateToNyquist(lane, in_span, in_offset);

        if (lane.state == PerSfDecoder::State::Detect) {
            auto r = lane.sync.tick(std::span<const cf32>(lane.nyquist_buf));
            if (r.state == phy::SyncResult::State::Locked) {
                // Minimum-SNR gate: reject weak-noise locks below
                // min_snr_db (default -19 dB). Clean synthetic test
                // frames routinely report 40-72 dB SNR.
                if (r.snr_db < min_snr_db) {
                    if (debug) {
                        gr::lora::log_ts("debug", "multisf",
                            "reject sf=%u bw=%u snr=%.1fdB below "
                            "min_snr_db=%.1f",
                            lane.sf, bandwidth, static_cast<double>(r.snr_db), static_cast<double>(min_snr_db));
                    }
                    lane.resetLane();
                    return lane.sps;
                }
                // CFO correction: apply PreambleSync estimate (cfo_int, cfo_frac)
                // to the downchirp reference so subsequent dechirp+FFT resolves
                // bins at the correct position. Without this, any carrier
                // frequency offset shifts the FFT argmax and corrupts both
                // header and payload decode.
                //
                // When AA filter is active, add filter group-delay offset
                // (6-stage cascade shifts dechirped peak by 12 bins — verified
                // in Python simulation).
                float       cfo_frac_corr = r.cfo_frac;
                int32_t     cfo_int_corr  = r.cfo_int;
                if (lane.aa_enabled) {
                    const int32_t filter_delay_bins = 12;
                    cfo_int_corr += filter_delay_bins;
                }
                lane.demod.set_cfo_correction(cfo_frac_corr, cfo_int_corr);
                lane.cached_snr_db = r.snr_db;
                // SyncResult noise_db/peak_db are raw FFT magnitudes;
                // convert to dBFS (20·log10) for the dashboard.
                lane.cached_noise_db  = (r.noise_db > 0.f) ? 20.f * std::log10(r.noise_db) : -999.f;
                lane.cached_peak_db   = (r.peak_db > 0.f) ? 20.f * std::log10(r.peak_db) : -999.f;
                lane.cached_cfo_int   = r.cfo_int;
                lane.cached_cfo_frac  = r.cfo_frac;
                lane.cached_sync_word = r.sync_word_observed; // kSyncWordUnknown in strict mode
                // SFO = (cfo_int + cfo_frac) * BW / Fc  (same formula as PreambleId)
                lane.cached_sfo_hat = (static_cast<float>(r.cfo_int) + r.cfo_frac) * static_cast<float>(bandwidth) / static_cast<float>(center_freq);

                // Sample-domain integer-STO realignment.
                //
                // PreambleSync.tick() consumes N Nyquist samples per symbol
                // tick. If the actual preamble is shifted in time by τ
                // samples, every symbol window is misaligned by τ samples
                // and the post-SFD payload demod sees garbage bins. We
                // realign by extending sfd_tail_remaining by τ sample-rate
                // units so the next OUTPUT-state dechirp window lands on
                // the actual symbol boundary.
                //
                // sto_int is the dechirp peak bin index in [0, N):
                //   sto_int <= N/2  →  τ = -sto_int  (preamble is EARLY)
                //   sto_int >  N/2  →  τ = N - sto_int  (preamble is LATE)
                //
                // Applied for both os=1 and os>1. At os=1 (wideband
                // ChannelSlot path after cascade decimation, and the
                // synthetic os=1 regression test) the adjustment is
                // strictly correct. At os>1 the oversampled-domain
                // scaling below keeps the correction valid.
                int32_t tail     = static_cast<int32_t>(lane.sps + lane.sps / 4u);
                int32_t tau_late = 0;
                if (lane.os_factor == 1u) {
                    const int32_t N_int = static_cast<int32_t>(lane.N);
                    if (r.sto_int > N_int / 2) {
                        tau_late = N_int - r.sto_int;
                    } else {
                        tau_late = -static_cast<int32_t>(r.sto_int);
                    }
                    tail += tau_late; // os_factor == 1 → no multiplier
                    if (tail < 0) {
                        tail = 0;
                    }
                } else {
                    // Combined integer + fractional STO correction for os > 1.
                    //
                    // WRAP sto_int + sto_frac into [-N/2, N/2) first, THEN
                    // scale by os_factor. This avoids the sto_int=255
                    // wrapping bug: sto_int=255 + sto_frac=0.25 at N=256
                    // represents -0.75 Nyquist samples (= 3 oversampled),
                    // but separate handling gives tau_late=4 (wrong).
                    const double os_d = static_cast<double>(lane.os_factor);
                    const double N_d  = static_cast<double>(lane.N);

                    double sto_total = static_cast<double>(r.sto_int) + static_cast<double>(r.sto_frac);
                    // PreambleSync's Eq. 20 uses the raw dechirped FFT
                    // (no CFO correction), so fractional CFO biases the
                    // fractional STO estimate by the same amount.
                    // Subtract cfo_frac to decouple: sto_frac_observed ≈
                    // sto_frac_true + cfo_frac (when both are small).
                    sto_total -= static_cast<double>(r.cfo_frac);
                    if (sto_total > N_d / 2.0) sto_total -= N_d;
                    if (sto_total < -N_d / 2.0) sto_total += N_d;

                    // Convert to oversampled-domain correction. Negate:
                    // positive sto_total = signal is early → shift grid
                    // backward (negative tau). Negative sto_total = signal
                    // is late → extend tail (positive tau).
                    const int32_t tau_total = static_cast<int32_t>(std::round(-sto_total * os_d));
                    tail += tau_total;
                    if (tail < 0) {
                        tail = 0;
                    }
                    // tau_total absorbs the full STO (integer + fractional),
                    // so decim_phase must be 0 to avoid double-compensation.
                    lane.decim_phase = 0;
                }
                lane.sfd_tail_remaining     = static_cast<uint32_t>(tail);
                lane.state                  = PerSfDecoder::State::SkipSfdTail;
                // Align stride decimation grid to fractional STO.
                // For os > 1: the combined formula above already sets
                // decim_phase = 0 (tau_total absorbs full STO).
                // For os = 1: decim_phase is always 0 (one sample per
                // Nyquist symbol), so no correction needed.
                // The legacy separate sto_frac-based decim_phase is
                // kept only for the os=1 path where it's harmless.
                if (lane.os_factor == 1u) {
                    auto frac_phase = std::round(-static_cast<double>(r.sto_frac) * static_cast<double>(lane.os_factor));
                    auto iphase      = static_cast<int32_t>(frac_phase) % static_cast<int32_t>(lane.os_factor);
                    if (iphase < 0) iphase += static_cast<int32_t>(lane.os_factor);
                    lane.decim_phase = static_cast<uint32_t>(iphase);
                }
                const uint32_t nominal_tail = lane.sps + lane.sps / 4u;
                if (debug) {
                    gr::lora::log_ts("debug", "multisf",
                        "LOCK sf=%u bw=%u os=%u sto_int=%d tau_late=%d "
                        "cfo_int=%d cfo_frac=%.3f sto_frac=%.3f "
                        "decim_phase=%u bin_hat=%u snr=%.1fdB "
                        "tail=%d(nom=%u)",
                        lane.sf, bandwidth, lane.os_factor, r.sto_int, tau_late, r.cfo_int, static_cast<double>(r.cfo_frac), static_cast<double>(r.sto_frac), lane.decim_phase, r.bin_hat, static_cast<double>(r.snr_db), tail, nominal_tail);
                }
                // Per-LOCK telemetry: published even when debug=false so that
                // hardware A/B harnesses (lora_test.py) can capture the
                // cfo_int / sto_int / tau_late distribution without enabling
                // a noisy log level. Schema mirrors the LOCK debug line above
                // plus the wire-friendly bw / os_factor fields. NEVER throw
                // from processBulk (gr4_block_rules.md "Silent scheduler
                // death") — swallow any exception in the user-supplied
                // telemetry callback so a misbehaving subscriber cannot kill
                // the scheduler.
                if (_telemetry) {
                    try {
                        gr::property_map evt;
                        evt["type"]         = std::pmr::string("multisf_sync");
                        evt["sf"]           = static_cast<uint32_t>(lane.sf);
                        evt["bw"]           = static_cast<uint32_t>(bandwidth);
                        evt["os_factor"]    = static_cast<uint32_t>(lane.os_factor);
                        evt["cfo_int"]      = static_cast<int32_t>(r.cfo_int);
                        evt["cfo_frac"]     = static_cast<double>(r.cfo_frac);
                        evt["sto_int"]      = static_cast<int32_t>(r.sto_int);
                        evt["tau_late"]     = static_cast<int32_t>(tau_late);
                        evt["snr_db"]       = static_cast<double>(r.snr_db);
                        evt["tail"]         = static_cast<int32_t>(tail);
                        evt["nominal_tail"] = static_cast<uint32_t>(nominal_tail);
                        _telemetry(evt);
                    } catch (...) {
                        // swallow — never propagate into the scheduler
                    }
                }
            } else if (r.state == phy::SyncResult::State::Failed) {
                lane.resetLane();
            }
            return lane.sps;
        }

        // State::Output — the SkipSfdTail case was handled above.
        assert(lane.state == PerSfDecoder::State::Output);

        // Stall safety.
        if (++lane.stall_symbols > kMaxStallSymbols) {
            if (debug) {
                gr::lora::log_ts("debug", "multisf", "STALL sf=%u bw=%u symbols=%u — resetting lane", lane.sf, bandwidth, lane.stall_symbols);
            }
            lane.resetLane();
            return lane.sps;
        }

        auto demod_result = lane.demod.demodHard(std::span<const cf32>(lane.nyquist_buf));
        // Per-symbol header diagnostic: log raw bin + PMR for first 8
        // symbols of each frame so we can compare tezuka (stride) vs
        // loopback (no stride) PMR distributions.
        const bool in_header = lane.output_symbol_count < 8;
        if (debug && in_header) {
            gr::lora::log_ts("debug", "multisf",
                "HDR_SYM sf=%u sym=%u raw_bin=%u pmr=%.2f snr=%.1fdB",
                lane.sf, lane.output_symbol_count, demod_result.bin,
                static_cast<double>(demod_result.pmr),
                static_cast<double>(lane.cached_snr_db));
        }
        ++lane.output_symbol_count;
        if (auto frame = lane.decode.push_symbol(demod_result.bin); frame.has_value()) {
            if (!frame->header_valid || frame->payload.empty()) {
                if (debug) {
                    uint16_t adj = (demod_result.bin == 0) ? static_cast<uint16_t>(lane.N - 1) : static_cast<uint16_t>(demod_result.bin - 1);
                    adj = static_cast<uint16_t>(adj ^ (adj >> 1));
                    gr::lora::log_ts("debug", "multisf",
                        "DROP sf=%u bw=%u header_valid=%d pay_len=%u "
                        "cr=%u crc=%d snr=%.1fdB raw_bin=%u adj_bin=%u",
                        lane.sf, bandwidth, frame->header_valid, static_cast<unsigned>(frame->payload_len), static_cast<unsigned>(frame->cr), frame->crc_valid, static_cast<double>(lane.cached_snr_db), demod_result.bin, adj);
                }
            }
            emitFrame(lane, *frame, out_span, out_idx);
            lane.resetLane();
        }
        return lane.sps;
    }

    static void decimateToNyquist(PerSfDecoder& lane, std::span<const cf32> in_span, std::size_t in_offset) noexcept {
        // Decimate from the oversampled buffer to Nyquist rate. During
        // Detect state, decim_phase is 0 (no timing info yet). After
        // LOCK, decim_phase is set to round(sto_frac * os) mod os so the
        // decimation picks samples at the correct phase within each
        // os-length stride, matching the signal's actual timing offset.
        if (!lane.aa_enabled) {
            const uint32_t os    = lane.os_factor;
            const uint32_t phase = lane.decim_phase; // 0 during Detect
            for (uint32_t i = 0; i < lane.N; ++i) {
                const std::size_t idx = in_offset + phase + static_cast<std::size_t>(i) * os;
                lane.nyquist_buf[i]   = (idx < in_span.size()) ? in_span[idx] : cf32{0.f, 0.f};
            }
            return;
        }

        // Anti-aliased path: feed one oversampled symbol (sps samples)
        // through the cascaded half-band FIR. The decim_phase offset is
        // applied BEFORE the filter — same sub-sample timing semantics
        // as the stride path — by advancing the input window. The phase
        // offset costs <= 1 sample in capture but preserves lock-point
        // timing across the post-Lock re-acquisition.
        const uint32_t    os    = lane.os_factor;
        const uint32_t    phase = lane.decim_phase;
        const std::size_t start = in_offset + phase;
        const std::size_t end   = std::min(start + lane.sps, in_span.size());
        // DEBUG: trace all AA calls to see if SF8 ever enters (disabled)
        if (false && lane.sf >= 8) {
            std::fprintf(stderr, "AA_ENTER sf=%u os=%u in_off=%zu phase=%u start=%zu end=%zu span=%zu sps=%u\n",
                lane.sf, os, in_offset, phase, start, end, in_span.size(), lane.sps);
            std::fflush(stderr);
        }
        if (end <= start) {
            // Not enough data; zero-fill Nyquist buffer as the stride
            // path would.
            std::fill(lane.nyquist_buf.begin(), lane.nyquist_buf.end(), cf32{0.f, 0.f});
            return;
        }
        auto window = in_span.subspan(start, end - start);

        lane.aa_out.clear();
        lane.aa_decim.processBatch(window, lane.aa_out);

        // processBatch emits floor(sps / os) ≈ N samples on steady-state
        // input; the first few calls after reset produce fewer due to the
        // stage delay-line fill. Zero-fill any tail short of N.
        if (false && lane.aa_out.size() < lane.N) {
            std::fprintf(stderr, "AA sf=%u os=%u in=%zu out=%zu need=%u zero_fill=%zu\n",
                lane.sf, lane.os_factor, window.size(), lane.aa_out.size(), lane.N, lane.N - lane.aa_out.size());
            std::fflush(stderr);
        }
        const auto n_copy = std::min<std::size_t>(lane.aa_out.size(), lane.N);
        std::copy_n(lane.aa_out.begin(), n_copy, lane.nyquist_buf.begin());
        // DEBUG: log Nyquist magnitude for SF7 vs SF8 comparison (disabled)
        if (false && n_copy > 0 && (lane.sf == 7 || lane.sf == 8)) {
            float sum_mag = 0.f;
            for (std::size_t j = 0; j < n_copy; ++j) sum_mag += std::abs(lane.nyquist_buf[j]);
            std::fprintf(stderr, "AA_MAG sf=%u os=%u avg_mag=%.6f first_mag=%.6f\n",
                lane.sf, os, static_cast<double>(sum_mag / static_cast<float>(n_copy)), static_cast<double>(std::abs(lane.nyquist_buf[0])));
            std::fflush(stderr);
        }
        if (n_copy < lane.N) {
            std::fill(lane.nyquist_buf.begin() + static_cast<std::ptrdiff_t>(n_copy), lane.nyquist_buf.end(), cf32{0.f, 0.f});
        }
        (void)os; // only used on stride path
    }

    void emitFrame(const PerSfDecoder& lane, const phy::FrameResult& frame, auto& out_span, std::size_t& out_idx) {
        if (!frame.header_valid || frame.payload.empty()) {
            return;
        }
        if (debug) {
            gr::lora::log_ts("debug", "multisf", "emit sf=%u bw=%u pay_len=%u cr=%u crc=%s snr=%.1fdB", lane.sf, bandwidth, static_cast<unsigned>(frame.payload_len), static_cast<unsigned>(frame.cr), frame.crc_valid ? "OK" : "FAIL", static_cast<double>(lane.cached_snr_db));
        }

        // Capture the position of the first payload byte BEFORE writing
        // so the per-frame tag lands on the frame's first sample.  When
        // two frames emit in one processBulk call, their tags are now
        // positioned distinctly (previous `this->publishTag(tag, 0)`
        // piled every tag at stream-position 0 and FrameSink
        // inputTagsPresent() would fire on the wrong byte).
        const std::size_t frame_start_idx = out_idx;

        // Copy payload bytes to the output port (stream).
        for (auto b : frame.payload) {
            if (out_idx >= out_span.size()) {
                break;
            }
            out_span[out_idx++] = b;
        }

        // Per-frame tag for downstream contract stability (FrameSink,
        // UDP emitter, monitoring dashboards).
        gr::property_map tag;
        tag["sf"]           = gr::pmt::Value(static_cast<int64_t>(lane.sf));
        tag["pay_len"]      = gr::pmt::Value(static_cast<int64_t>(frame.payload_len));
        tag["cr"]           = gr::pmt::Value(static_cast<int64_t>(frame.cr));
        tag["crc_valid"]    = gr::pmt::Value(frame.crc_valid);
        tag["is_downchirp"] = gr::pmt::Value(false);
        tag["snr_db"]       = gr::pmt::Value(static_cast<double>(lane.cached_snr_db));
        if (lane.cached_noise_db > -999.f) {
            tag["noise_floor_db"] = gr::pmt::Value(static_cast<double>(lane.cached_noise_db));
        }
        if (lane.cached_peak_db > -999.f) {
            tag["peak_db"] = gr::pmt::Value(static_cast<double>(lane.cached_peak_db));
        }
        tag["cfo_int"]  = gr::pmt::Value(static_cast<int64_t>(lane.cached_cfo_int));
        tag["cfo_frac"] = gr::pmt::Value(static_cast<double>(lane.cached_cfo_frac));
        tag["sfo_hat"]  = gr::pmt::Value(static_cast<double>(lane.cached_sfo_hat));

        // LDRO detection — rule-based config the decoder actually used +
        // statistical LSB-zero signature from raw payload bins.
        // `ldro_detected` trips at fraction >= 0.75 (ideal 1.0 vs uniform
        // 0.5; 0.75 keeps the false-positive rate below 1% at N=32 under
        // Bin(n, 0.5)). `ldro_match == false` is an alarm: decoder used
        // the wrong interleaver shape and the CRC almost certainly
        // failed. Gated on N >= 16 for statistical power — shorter
        // payloads only surface `ldro_cfg` on the tag.
        tag["ldro_cfg"] = gr::pmt::Value(frame.ldro_cfg);
        if (frame.payload_syms_seen >= 16) {
            const double frac         = static_cast<double>(frame.payload_lsb0_hits) / static_cast<double>(frame.payload_syms_seen);
            const bool   detected     = (frac >= 0.75);
            tag["ldro_lsb0_fraction"] = gr::pmt::Value(frac);
            tag["ldro_n_samples"]     = gr::pmt::Value(static_cast<int64_t>(frame.payload_syms_seen));
            tag["ldro_detected"]      = gr::pmt::Value(detected);
            tag["ldro_match"]         = gr::pmt::Value(detected == frame.ldro_cfg);
        }
        // Promiscuous mode: surface the observed sync_word on the frame tag so
        // FrameSink uses it (overriding its configured sync_word property).
        if (lane.cached_sync_word != phy::SyncResult::kSyncWordUnknown) {
            tag["sync_word"] = gr::pmt::Value(static_cast<int64_t>(lane.cached_sync_word));
        }
        if (rx_channel >= 0) {
            tag["rx_channel"] = gr::pmt::Value(static_cast<int64_t>(rx_channel));
        }
        // Timing tags from upstream ppm_estimator (see _last_* cache).
        // sample_rate must stay float32 (SigMF tag::SAMPLE_RATE); frequency is
        // float64 (SigMF tag::FREQUENCY); ppm_error is a custom float64 tag.
        // FrameSink (Task 7) consumes sample_rate as float, the others as double.
        if (_last_sample_rate > 0.f) {
            tag["sample_rate"] = gr::pmt::Value(_last_sample_rate); // SigMF tag::SAMPLE_RATE is float32
        }
        if (_last_frequency != 0.0) {
            tag["frequency"] = gr::pmt::Value(_last_frequency);
        }
        if (_last_ppm != 0.f) {
            tag["ppm_error"] = gr::pmt::Value(static_cast<double>(_last_ppm));
        }
        // Publish via OutputSpan (pre-reserved tags slot) — NOT
        // `this->publishTag(...)`.  Upstream PR #439/#625 Writer is
        // single-reserve-per-lifetime; Block::publishTag from processBulk
        // would call Port::publishTag -> tagWriter().tryReserve(1) and
        // trip the SingleWriter abort (OutputSpan::tags already holds a
        // reservation for the entire tag buffer).
        out_span.publishTag(tag, frame_start_idx);

        // Per-frame message (FrameSink consumes this).
        std::string      payload_str(frame.payload.begin(), frame.payload.end());
        gr::property_map msg_data;
        msg_data["payload"]      = gr::pmt::Value(payload_str);
        msg_data["crc_valid"]    = gr::pmt::Value(frame.crc_valid);
        msg_data["is_downchirp"] = gr::pmt::Value(false);
        gr::sendMessage<gr::message::Command::Notify>(msg_out, "", "payload", msg_data);

        // Optional telemetry callback (set externally).
        if (_telemetry) {
            try {
                gr::property_map evt;
                evt["type"]   = std::pmr::string("multisf_frame");
                evt["sf"]     = static_cast<uint32_t>(lane.sf);
                evt["crc_ok"] = frame.crc_valid;
                evt["len"]    = static_cast<uint32_t>(frame.payload_len);
                evt["cr"]     = static_cast<uint32_t>(frame.cr);
                evt["snr_db"] = static_cast<double>(lane.cached_snr_db);
                _telemetry(evt);
            } catch (...) {
                // swallow — never propagate exceptions into the scheduler
            }
        }
    }
};

} // namespace gr::lora

GR_REGISTER_BLOCK("gr::lora::MultiSfDecoder", gr::lora::MultiSfDecoder)

#endif // GNURADIO_LORA_MULTI_SF_DECODER_HPP
