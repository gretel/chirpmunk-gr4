// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_PHY_PREAMBLE_SYNC_HPP
#define GNURADIO_LORA_PHY_PREAMBLE_SYNC_HPP

// PreambleSync implements the full iterative Xhonneux §6 sync algorithm.
//
// Reference: ~/.config/opencode/skills/lora-sdr-impl/references/xhonneux_sync.md
//
// Stages (ticks consumed at Nyquist rate, one N-sample symbol per tick):
//
//   Stage 1 — DetectU1U3:
//     Sliding-window dechirp+FFT against the base downchirp. Vangelista α
//     threshold for "signal present". When 3 consecutive ticks produce
//     argmax bins within ±1 of each other, declare detection and store the
//     last 3 dechirped FFT vectors as U1..U3.
//
//   Stage 2 — EstimateU4U6:
//     Collect 3 more upchirp DFTs (U4..U6). Compute Y^avg = (U4+U5+U6)/3.
//     Compute λ̂_CFO via Eq. 14 (cross-correlation of consecutive upchirps
//     in a 5-bin window around the peak). Compute preliminary λ̃_STO via
//     Eq. 20 with M̃ = N − s̃_up using Y^avg. Save Y^avg for Stage 4.
//
//   Stage 3 — IntegerU7..D1:
//     Consume U7 (= 1 tick) → dechirp with downchirp ref → s_up.
//     Skip 2 ticks (NID1, NID2 — sync word, not used by sync).
//     Consume D1 (= 1 tick) → dechirp with upchirp ref → s_down.
//     Compute L̂_CFO = Γ_N[(s_up + s_down) mod N] / 2.
//     Compute L̂_STO = (s_up − L̂_CFO) mod N.
//
//   Stage 4 — Refine (no new samples):
//     Recompute λ̂_STO via Eq. 20 with M̂ = N − L̂_STO and stored Y^avg.
//     Populate the final SyncResult and transition to Locked.
//
// Failure modes (transition to State::Failed):
//   - Stage 1 timeout (> max_detect_ticks ticks without detection)
//   - Vangelista threshold never passes
//   - |λ̂_CFO| > 0.5  (out of estimable range)
//   - |L̂_CFO| > N/4  (out of estimable range per §5.2)
//
// Inputs are Nyquist-rate complex samples (N = 2^sf per tick). The driving
// block wrapper is responsible for decimating from any oversampled stream
// before invoking tick(). Sub-sample STO realignment (using sto_frac on the
// returned SyncResult) also happens in the block wrapper.

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <numbers>
#include <span>
#include <vector>

#include <gnuradio-4.0/lora/algorithm/utilities.hpp>
#include <gnuradio-4.0/lora/detail/ChirpRefs.hpp>
#include <gnuradio-4.0/lora/detail/FftPool.hpp>
#include <gnuradio-4.0/lora/phy/Types.hpp>
#include <gnuradio-4.0/lora/scan/VangelistaThreshold.hpp>

namespace gr::lora::phy {

class PreambleSync {
public:
    struct Config {
        uint8_t  sf               = 7;
        uint32_t bandwidth_hz     = 125000; ///< informational
        uint16_t preamble_len     = 8;      ///< informational; sync uses 7 upchirps
        uint16_t sync_word        = 0x12;   ///< expected sync word (ignored when promiscuous)
        double   p_false_alarm    = 0.01;   ///< Vangelista α target
        uint32_t max_detect_ticks = 1000;   ///< Stage 1 timeout
        bool     verify_threshold = true;   ///< apply Vangelista α gate in Stage 1
        /// Promiscuous mode — skip the stage-4 sync_word compare and instead
        /// decode the observed sync_word from the NID1/NID2 bin positions,
        /// surfacing it as SyncResult::sync_word_observed.  Lets a single
        /// decoder lock onto any sync word (MeshCore 0x12, LoRaWAN 0x34,
        /// private nets, etc.) at the cost of a slightly higher false-lock
        /// rate (all of which still fail at CRC / length plausibility).
        bool promiscuous = false;
    };

    void init(const Config& cfg) {
        _cfg = cfg;
        _N   = uint32_t{1} << cfg.sf;

        auto refs      = detail::build_ref_pair(cfg.sf, /*os_factor=*/1);
        _upchirp_ref   = std::move(refs.first);
        _downchirp_ref = std::move(refs.second);

        _scratch.assign(_N, cf32{0.f, 0.f});
        _Y_avg.assign(_N, cf32{0.f, 0.f});

        _alpha = scan::alpha_for_sf(cfg.sf, cfg.p_false_alarm);

        // Save the last 3 detection FFTs for Eq. 14 cross-correlation in
        // case we want to use them later. Currently we recompute over U4-U6.
        _Y_history.assign(3, std::vector<cf32>(_N, cf32{0.f, 0.f}));
        _bin_history.assign(3, 0u);

        reset();
    }

    void reset() noexcept {
        _stage        = Stage::DetectU1U3;
        _detect_count = 0;
        _detect_ticks = 0;
        _bin_last     = UINT32_MAX;
        _est_count    = 0;
        _result       = SyncResult{};
        _result.state = SyncResult::State::Detecting;
        _peak_db      = 0.f;
        _noise_db     = 0.f;
        std::ranges::fill(_Y_avg, cf32{0.f, 0.f});
    }

    /// Push one symbol's worth of Nyquist-rate samples (length N).
    /// Returns the current SyncResult (state may have advanced).
    [[nodiscard]] SyncResult tick(std::span<const cf32> nyquist_samples) noexcept {
        if (nyquist_samples.size() < _N) {
            return _result; // not enough input; caller must accumulate
        }
        _result.samples_consumed = _N;

        switch (_stage) {
        case Stage::DetectU1U3: runDetect(nyquist_samples); break;
        case Stage::EstimateU4U6: runEstimate(nyquist_samples); break;
        case Stage::SkipExtraUp: runSkipExtraUp(); break;
        case Stage::IntegerU7: runIntegerU7(nyquist_samples); break;
        case Stage::IntegerSkip1: runIntegerNid(nyquist_samples, true); break;
        case Stage::IntegerSkip2: runIntegerNid(nyquist_samples, false); break;
        case Stage::IntegerD1: runIntegerD1(nyquist_samples); break;
        case Stage::Locked: break;
        case Stage::Failed: break;
        }
        return _result;
    }

    [[nodiscard]] uint8_t  sf() const noexcept { return _cfg.sf; }
    [[nodiscard]] uint32_t fft_size() const noexcept { return _N; }
    [[nodiscard]] bool     locked() const noexcept { return _stage == Stage::Locked; }
    [[nodiscard]] bool     failed() const noexcept { return _stage == Stage::Failed; }

private:
    enum class Stage : uint8_t {
        DetectU1U3,   // Stage 1: 3 matching upchirp argmax
        EstimateU4U6, // Stage 2: λ̂_CFO + preliminary λ̃_STO
        SkipExtraUp,  // Stage 2.5: consume intermediate preamble upchirps
        IntegerU7,    // Stage 3a: dechirp upchirp -> s_up
        IntegerSkip1, // skip NID1
        IntegerSkip2, // skip NID2
        IntegerD1,    // Stage 3b: dechirp downchirp -> s_down + Stage 4 refine
        Locked,
        Failed,
    };

    Config     _cfg;
    uint32_t   _N     = 0;
    Stage      _stage = Stage::DetectU1U3;
    SyncResult _result;
    double     _alpha = 0.0;

    // Reference chirps (id=0 upchirp and its conjugate downchirp)
    std::vector<cf32> _upchirp_ref;
    std::vector<cf32> _downchirp_ref;

    // FFT scratch
    std::vector<cf32> _scratch;

    // Stage 1 state
    uint32_t                       _detect_count = 0;
    uint32_t                       _detect_ticks = 0;
    uint32_t                       _bin_last     = UINT32_MAX;
    std::vector<std::vector<cf32>> _Y_history;
    std::vector<uint32_t>          _bin_history;

    // Stage 2 state: averaged dechirped FFT over U4..U6 + preliminary STO
    uint32_t          _est_count = 0;
    std::vector<cf32> _Y_avg;
    float             _cfo_frac_est    = 0.f;
    float             _sto_frac_prelim = 0.f;
    uint32_t          _bin_at_estimate = 0; // s̃_up (for M̃)

    // Stage 2.5 state: for preamble_len > 7, consume the extra upchirps
    // between U6 (end of averaging) and U_last (s_up measurement).
    uint32_t _extra_up_remaining = 0;

    // Stage 3 state
    uint32_t _s_up     = 0;
    uint32_t _s_down   = 0;
    uint32_t _nid1_bin = 0; ///< NID1 argmax bin (pre-CFO correction)
    uint32_t _nid2_bin = 0; ///< NID2 argmax bin (pre-CFO correction)

    // Diagnostics
    float _peak_db  = 0.f;
    float _noise_db = 0.f;

    // ===== Stage runners =====

    void runDetect(std::span<const cf32> samples) {
        ++_detect_ticks;
        if (_detect_ticks > _cfg.max_detect_ticks) {
            failWith();
            return;
        }

        // Dechirp with downchirp ref → FFT → argmax + magnitude vector.
        std::vector<cf32> Y(_N);
        const auto        stats = dechirpAndFft(samples.data(), _downchirp_ref.data(), _scratch.data(), _N, Y.data());

        // Vangelista α gate (optional, skipped in synthetic tests if disabled).
        if (_cfg.verify_threshold) {
            std::vector<float> mags(_N);
            for (uint32_t i = 0; i < _N; ++i) {
                mags[i] = std::abs(Y[i]);
            }
            if (!scan::detect(std::span<const float>(mags), _alpha)) {
                resetDetectChain();
                _result.state = SyncResult::State::Detecting;
                return;
            }
        }

        const uint32_t bin = stats.argmax_bin;

        if (_detect_count > 0 && _bin_last != UINT32_MAX) {
            const int32_t diff  = static_cast<int32_t>(bin) - static_cast<int32_t>(_bin_last);
            const int32_t adiff = std::abs(diff);
            if (adiff > 1) {
                resetDetectChain();
            }
        }
        _bin_last = bin;
        _detect_count++;

        // Push into rolling history (last 3 entries).
        for (std::size_t i = 0; i + 1 < _Y_history.size(); ++i) {
            _Y_history[i]   = _Y_history[i + 1];
            _bin_history[i] = _bin_history[i + 1];
        }
        _Y_history.back()   = std::move(Y);
        _bin_history.back() = bin;

        _result.bin_hat  = bin;
        _result.peak_db  = stats.peak_mag;
        _result.noise_db = stats.mean_mag;
        // SNR formula:
        //   SNR_dB = 10·log10(peak_power / (total_power − peak_power))
        // i.e. peak power divided by the noise-bin power (peak excluded
        // from the noise estimate). The alternative peak_mag/mean_mag
        // ratio would report absurd values ("SNR = 401 dB") on clean
        // signals since mean_mag ≈ peak_mag/N.
        const float noise_power = stats.total_power - stats.peak_power;
        _result.snr_db          = (noise_power > 0.f) ? 10.f * std::log10(stats.peak_power / noise_power) : 0.f;
        _result.state           = SyncResult::State::Detecting;

        if (_detect_count >= 3) {
            _stage     = Stage::EstimateU4U6;
            _est_count = 0;
            std::ranges::fill(_Y_avg, cf32{0.f, 0.f});
            _bin_at_estimate = bin; // s̃_up (initial)
            _result.state    = SyncResult::State::Syncing;
        }
    }

    void runEstimate(std::span<const cf32> samples) {
        std::vector<cf32> Y(_N);
        const auto        stats = dechirpAndFft(samples.data(), _downchirp_ref.data(), _scratch.data(), _N, Y.data());
        (void)stats;

        // Accumulate into Y_avg and history.
        for (uint32_t i = 0; i < _N; ++i) {
            _Y_avg[i] += Y[i];
        }
        for (std::size_t i = 0; i + 1 < _Y_history.size(); ++i) {
            _Y_history[i]   = _Y_history[i + 1];
            _bin_history[i] = _bin_history[i + 1];
        }
        _Y_history.back()   = std::move(Y);
        _bin_history.back() = stats.argmax_bin;
        ++_est_count;

        if (_est_count < 3) {
            _result.state = SyncResult::State::Syncing;
            return;
        }

        // Stage 2 complete: average and compute estimates.
        for (uint32_t i = 0; i < _N; ++i) {
            _Y_avg[i] /= 3.f;
        }

        // λ̂_CFO via Eq. 14: 5-bin window cross-correlation across 2 pairs.
        // i = argmax of the most recent dechirped FFT (or Y_avg). Use Y_avg.
        uint32_t i_peak  = 0;
        float    max_mag = 0.f;
        for (uint32_t k = 0; k < _N; ++k) {
            const float m = std::abs(_Y_avg[k]);
            if (m > max_mag) {
                max_mag = m;
                i_peak  = k;
            }
        }
        _bin_at_estimate = i_peak;

        cf32 acc{0.f, 0.f};
        for (std::size_t l = 1; l < _Y_history.size(); ++l) {
            const auto& Yl   = _Y_history[l];
            const auto& Ylm1 = _Y_history[l - 1];
            for (int p = -2; p <= 2; ++p) {
                const int32_t kk = static_cast<int32_t>(i_peak) + p;
                if (kk < 0 || static_cast<uint32_t>(kk) >= _N) {
                    continue;
                }
                const auto kkz = static_cast<std::size_t>(kk);
                acc += Yl[kkz] * std::conj(Ylm1[kkz]);
            }
        }
        const float cfo_frac = (std::abs(acc) > 0.f) ? std::arg(acc) / (2.f * std::numbers::pi_v<float>) : 0.f;

        if (std::abs(cfo_frac) >= 0.5f) {
            failWith();
            return;
        }
        _cfo_frac_est = cfo_frac;

        // Preliminary λ̃_STO via Eq. 20 with M̃ = N − s̃_up using Y_avg.
        _sto_frac_prelim = computeStoFracEq20(_Y_avg.data(), _N, i_peak, static_cast<int32_t>(_N) - static_cast<int32_t>(i_peak));

        _result.cfo_frac = _cfo_frac_est;
        _result.sto_frac = _sto_frac_prelim;
        _result.bin_hat  = i_peak;
        _result.state    = SyncResult::State::Syncing;

        // Preamble length accounting: Stage 1 consumed 3 upchirps (U1..U3),
        // Stage 2 consumed 3 (U4..U6), Stage 3 will consume 1 final upchirp.
        // For preamble_len > 7 we need to drop the remainder before the
        // final s_up measurement.
        if (_cfg.preamble_len > 7u) {
            _extra_up_remaining = static_cast<uint32_t>(_cfg.preamble_len) - 7u;
            _stage              = Stage::SkipExtraUp;
        } else {
            _extra_up_remaining = 0;
            _stage              = Stage::IntegerU7;
        }
    }

    void runSkipExtraUp() {
        if (_extra_up_remaining > 0) {
            --_extra_up_remaining;
        }
        if (_extra_up_remaining == 0) {
            _stage = Stage::IntegerU7;
        }
        _result.state = SyncResult::State::Syncing;
    }

    void runIntegerU7(std::span<const cf32> samples) {
        std::vector<cf32> Y(_N);
        const auto        stats = dechirpAndFft(samples.data(), _downchirp_ref.data(), _scratch.data(), _N, Y.data());
        _s_up                   = stats.argmax_bin;
        _stage                  = Stage::IntegerSkip1;
        _result.state           = SyncResult::State::Syncing;
    }

    /// Dechirp+FFT the NID1 or NID2 sample window and save the argmax
    /// bin for later sync word verification in runIntegerD1. NID1/NID2
    /// are upchirps (the sync word is encoded in the position of the
    /// peak within an upchirp), so dechirp against the downchirp ref.
    void runIntegerNid(std::span<const cf32> samples, bool is_nid1) {
        std::vector<cf32> Y(_N);
        const auto        stats = dechirpAndFft(samples.data(), _downchirp_ref.data(), _scratch.data(), _N, Y.data());
        if (is_nid1) {
            _nid1_bin = stats.argmax_bin;
            _stage    = Stage::IntegerSkip2;
        } else {
            _nid2_bin = stats.argmax_bin;
            _stage    = Stage::IntegerD1;
        }
        _result.state = SyncResult::State::Syncing;
    }

    void runIntegerD1(std::span<const cf32> samples) {
        // Downchirp signal is dechirped against the upchirp reference (x_0).
        std::vector<cf32> Y(_N);
        const auto        stats = dechirpAndFft(samples.data(), _upchirp_ref.data(), _scratch.data(), _N, Y.data());
        _s_down                 = stats.argmax_bin;

        // L̂_CFO via Γ_N[(s_up + s_down) mod N] / 2.
        const uint32_t sum_mod = (_s_up + _s_down) % _N;
        int32_t        gamma_N;
        if (sum_mod < _N / 2) {
            gamma_N = static_cast<int32_t>(sum_mod);
        } else {
            gamma_N = static_cast<int32_t>(sum_mod) - static_cast<int32_t>(_N);
        }
        // The half-divide must round to nearest integer (paper uses /2 of an
        // integer that should be even when both s_up and s_down are correct;
        // an off-by-one rounding here propagates to L̂_STO).
        const int32_t L_cfo = gamma_N / 2;

        if (std::abs(L_cfo) > static_cast<int32_t>(_N / 4)) {
            failWith();
            return;
        }

        // L̂_STO = (s_up − L̂_CFO) mod N.
        int32_t L_sto = (static_cast<int32_t>(_s_up) - L_cfo) % static_cast<int32_t>(_N);
        if (L_sto < 0) {
            L_sto += static_cast<int32_t>(_N);
        }

        // Sync word verification. Without it, 3 consecutive matching
        // upchirps would unconditionally count as a lock — including
        // noise-correlation tracks and cross-SF spurs. Verifying the
        // NID1/NID2 bins decode to the configured sync_word eliminates
        // these false locks.
        //
        // Sync word encoding: for sync_word = 0xAB, NID1 lands at
        // bin (A << 3) and NID2 at bin (B << 3). Apply the integer
        // CFO correction (subtract L_cfo mod N) and the integer STO
        // correction (subtract L_sto mod N) to both NID bins to get
        // their canonical positions.  In strict mode (default) we
        // compare against the configured sync_word with ±2 bin
        // tolerance.  In promiscuous mode we instead decode the
        // observed sync_word from the corrected positions and accept
        // the lock.
        {
            const int32_t N_int   = static_cast<int32_t>(_N);
            const auto    mod_pos = [N_int](int32_t v) {
                int32_t r = v % N_int;
                if (r < 0) {
                    r += N_int;
                }
                return r;
            };
            // NID peak position = (sync_word_bin + cfo_int + sto_int) mod N.
            // Invert by subtracting both cfo_int and sto_int.
            const int32_t nid1_corrected = mod_pos(static_cast<int32_t>(_nid1_bin) - L_cfo - L_sto);
            const int32_t nid2_corrected = mod_pos(static_cast<int32_t>(_nid2_bin) - L_cfo - L_sto);

            if (_cfg.promiscuous) {
                // Decode the observed sync_word from the NID bin positions.
                // Quantize to the nearest /8 grid (the encoding is `nibble << 3`)
                // and wrap into [0..15] for each nibble.  Rounding add = 4 to
                // snap to nearest multiple of 8.
                const uint32_t hi          = static_cast<uint32_t>(((nid1_corrected + 4) >> 3)) & 0x0Fu;
                const uint32_t lo          = static_cast<uint32_t>(((nid2_corrected + 4) >> 3)) & 0x0Fu;
                _result.sync_word_observed = (hi << 4) | lo;
            } else {
                const int32_t sw0 = static_cast<int32_t>((_cfg.sync_word & 0xF0u) >> 4u) << 3;
                const int32_t sw1 = static_cast<int32_t>(_cfg.sync_word & 0x0Fu) << 3;

                // Circular distance in [-N/2, N/2-1] — account for wraparound.
                const auto circ_dist = [N_int](int32_t a, int32_t b) {
                    int32_t d = std::abs(a - b);
                    if (d > N_int / 2) {
                        d = N_int - d;
                    }
                    return d;
                };
                constexpr int32_t kSyncWordBinTolerance = 2;
                if (circ_dist(nid1_corrected, sw0) > kSyncWordBinTolerance || circ_dist(nid2_corrected, sw1) > kSyncWordBinTolerance) {
                    failWith();
                    return;
                }
            }
        }

        // Stage 4 — Refine λ̂_STO with M̂ = N − L̂_STO using stored Y^avg.
        const float sto_frac_refined = computeStoFracEq20(_Y_avg.data(), _N, _bin_at_estimate, static_cast<int32_t>(_N) - L_sto);

        _result.cfo_int  = L_cfo;
        _result.sto_int  = L_sto;
        _result.cfo_frac = _cfo_frac_est;
        _result.sto_frac = sto_frac_refined;
        _result.state    = SyncResult::State::Locked;
        _stage           = Stage::Locked;
    }

    void resetDetectChain() noexcept {
        // Reset to 0 (not 1): runDetect unconditionally increments
        // _detect_count after the reset path, so 0 makes the current
        // tick count as the FIRST in the new chain after increment.
        // Setting 1 here would cause Stage 1 to fire on 2 matching
        // upchirps instead of 3 after a leading non-matching tick.
        // Regression test: "Stage 1 fires on 3 consecutive upchirps
        // even after leading non-matching tick".
        _detect_count = 0;
    }

    void failWith() noexcept {
        _stage        = Stage::Failed;
        _result.state = SyncResult::State::Failed;
    }

    // ===== Helpers =====

    struct DechirpStats {
        uint32_t argmax_bin;
        float    peak_mag;    ///< max |Y[k]|
        float    mean_mag;    ///< average |Y[k]| over all bins (incl. peak)
        float    peak_power;  ///< max |Y[k]|² (for SNR calc)
        float    total_power; ///< sum of |Y[k]|² over all bins
    };

    static DechirpStats dechirpAndFft(const cf32* samples, const cf32* ref, cf32* scratch, uint32_t N, cf32* Y_out) {
        for (uint32_t i = 0; i < N; ++i) {
            scratch[i] = samples[i] * ref[i];
        }
        auto&    fft  = detail::FftPool::acquire(static_cast<uint8_t>(std::log2(static_cast<double>(N))));
        auto     out  = fft.compute(std::span<const cf32>(scratch, N));
        float    peak = 0.f, total = 0.f;
        float    peak_power = 0.f, total_power = 0.f;
        uint32_t peak_idx = 0;
        for (uint32_t i = 0; i < N; ++i) {
            Y_out[i]      = out[i];
            const float m = std::abs(out[i]);
            const float p = out[i].real() * out[i].real() + out[i].imag() * out[i].imag();
            total += m;
            total_power += p;
            if (m > peak) {
                peak       = m;
                peak_power = p;
                peak_idx   = i;
            }
        }
        return {peak_idx, peak, total / static_cast<float>(N), peak_power, total_power};
    }

    /// Eq. 20: λ̂_STO = -Re[(e_pos·Y_{i+1} - e_neg·Y_{i-1}) /
    ///                     (2·Y_i - e_pos·Y_{i+1} - e_neg·Y_{i-1})]
    /// where e_pos = exp(-j·2π·M_hat/N), e_neg = exp(+j·2π·M_hat/N).
    static float computeStoFracEq20(const cf32* Y, uint32_t N, uint32_t i_peak, int32_t M_hat) {
        const auto i_minus = (i_peak == 0) ? (N - 1) : (i_peak - 1);
        const auto i_plus  = (i_peak + 1) % N;

        const float arg = 2.f * std::numbers::pi_v<float> * static_cast<float>(M_hat) / static_cast<float>(N);
        const cf32  e_pos{std::cos(-arg), std::sin(-arg)};
        const cf32  e_neg{std::cos(arg), std::sin(arg)};

        const cf32 num = e_pos * Y[i_plus] - e_neg * Y[i_minus];
        const cf32 den = 2.f * Y[i_peak] - e_pos * Y[i_plus] - e_neg * Y[i_minus];
        if (std::abs(den) < 1e-12f) {
            return 0.f;
        }
        const cf32  ratio = num / den;
        const float val   = -ratio.real();
        // Clamp to (-0.5, 0.5] to keep the estimator in range.
        if (val > 0.5f) {
            return 0.5f;
        }
        if (val < -0.5f) {
            return -0.5f;
        }
        return val;
    }
};

} // namespace gr::lora::phy

#endif // GNURADIO_LORA_PHY_PREAMBLE_SYNC_HPP
