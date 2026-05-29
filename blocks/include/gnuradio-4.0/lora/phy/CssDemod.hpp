// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_PHY_CSS_DEMOD_HPP
#define GNURADIO_LORA_PHY_CSS_DEMOD_HPP

// CssDemod is a stateless per-symbol demodulator: dechirp + FFT + argmax for
// the hard path, magnitude-squared vector for the soft path. The CFO-
// corrected downchirp reference is held internally and rebuilt by
// set_cfo_correction(); the FFT instance is shared via FftPool.
//
// Returned DemodResult.bin is the raw FFT argmax index in [0, 2^sf). The
// downstream DecodeChain undoes the +1 cyclic offset and Gray demapping.
// CssDemod itself does NOT touch the bin index.

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <numbers>
#include <span>
#include <vector>

#include <gnuradio-4.0/lora/algorithm/GrayPartition.hpp>
#include <gnuradio-4.0/lora/algorithm/utilities.hpp>
#include <gnuradio-4.0/lora/detail/ChirpRefs.hpp>
#include <gnuradio-4.0/lora/detail/FftPool.hpp>
#include <gnuradio-4.0/lora/phy/Types.hpp>

namespace gr::lora::phy {

class CssDemod {
public:
    struct Config {
        uint8_t  sf           = 7;
        uint32_t bandwidth_hz = 125000; ///< reserved (currently unused; FFT size is 2^sf)
        bool     soft_decode  = false;  ///< if true, demodSoft populates llr_out
    };

    void init(const Config& cfg) {
        _cfg = cfg;
        _N   = uint32_t{1} << cfg.sf;

        _scratch.assign(_N, cf32{0.f, 0.f});
        _mag_sq.assign(_N, 0.f);
        _sym_mag_sq.assign(_N, 0.f);

        // Default downchirp = id=0 conjugate (no CFO correction).
        _cfo_downchirp = detail::build_downchirp(cfg.sf, /*os_factor=*/1);

        if (cfg.soft_decode) {
            _gp.init(cfg.sf);
        }
    }

    /// Rebuild the CFO-corrected downchirp reference. Call once per frame
    /// after PreambleSync produces (cfo_int, cfo_frac). Subsequent demodHard
    /// / demodSoft calls use the corrected reference.
    ///
    /// Math:
    ///   uc[n]            = upchirp(id = cfo_int mod N, sf, os=1)[n]
    ///   downchirp[n]     = conj(uc[n])
    ///   cfo_downchirp[n] = downchirp[n] * exp(-j * 2π * cfo_frac * n / N)
    void set_cfo_correction(float cfo_frac, int32_t cfo_int) {
        const int32_t cfo_int_mod = ((cfo_int % static_cast<int32_t>(_N)) + static_cast<int32_t>(_N)) % static_cast<int32_t>(_N);
        auto          uc          = detail::build_upchirp(_cfg.sf, /*os_factor=*/1, static_cast<uint32_t>(cfo_int_mod));

        _cfo_downchirp.assign(_N, cf32{0.f, 0.f});
        for (uint32_t n = 0; n < _N; ++n) {
            _cfo_downchirp[n] = std::conj(uc[n]);
        }

        const float frac_scale = -2.f * std::numbers::pi_v<float> * cfo_frac / static_cast<float>(_N);
        for (uint32_t n = 0; n < _N; ++n) {
            const float phase = frac_scale * static_cast<float>(n);
            _cfo_downchirp[n] *= cf32{std::cos(phase), std::sin(phase)};
        }
    }

    /// Reset the CFO-corrected downchirp to id=0 conjugate (no CFO).
    void reset_cfo_correction() noexcept { _cfo_downchirp = detail::build_downchirp(_cfg.sf, 1); }

    /// Hard-decision demodulation: returns the raw FFT argmax bin and PMR.
    [[nodiscard]] DemodResult demodHard(std::span<const cf32> symbol) noexcept {
        DemodResult r{};
        if (symbol.size() < _N) {
            return r; // insufficient samples
        }
        auto&      fft = detail::FftPool::acquire(_cfg.sf);
        const auto dr  = gr::lora::dechirp_and_quality(symbol.data(), _cfo_downchirp.data(), _scratch.data(), _N, fft,
            /*remove_dc=*/false);
        r.bin          = static_cast<uint16_t>(dr.bin);
        r.pmr          = dr.pmr;
        // peak_mag_sq is diagnostic-only (not used by DecodeChain). Left at 0;
        // dechirp_and_quality returns magnitudes rather than mag².
        r.peak_mag_sq = 0.f;
        return r;
    }

    /// Soft-decision demodulation: dechirp+FFT, compute per-bit max-log LLRs.
    /// llr_out must have length sf. Returns the hard-decision bin alongside
    /// (so callers can pipe the bin into DecodeChain in lock-step with LLRs).
    [[nodiscard]] DemodResult demodSoft(std::span<const cf32> symbol, std::span<double> llr_out) noexcept {
        DemodResult r{};
        if (symbol.size() < _N || llr_out.size() < _cfg.sf) {
            return r;
        }
        auto&      fft = detail::FftPool::acquire(_cfg.sf);
        const auto dr  = gr::lora::dechirp_soft(symbol.data(), _cfo_downchirp.data(), _scratch.data(), _N, fft, _mag_sq.data());
        r.bin          = static_cast<uint16_t>(dr.bin);
        r.pmr          = dr.pmr;
        r.peak_mag_sq  = _mag_sq[dr.bin];

        // Remap mag² from FFT bin order to symbol order: symbol s maps to
        // FFT bin (s+1) mod N (the +1 offset baked into TX gray_demap).
        for (uint32_t s = 0; s < _N; ++s) {
            _sym_mag_sq[s] = _mag_sq[(s + 1) % _N];
        }

        // Per-bit LLRs via the max-log helper below. Writes sf doubles.
        // Note: GrayPartition must be initialized (soft_decode=true) at init.
        if (_gp.sf == _cfg.sf) {
            computeLlrInto(_sym_mag_sq.data(), _N, _cfg.sf, _gp, llr_out.data());
        } else {
            std::ranges::fill(llr_out, 0.0);
        }
        return r;
    }

    [[nodiscard]] uint8_t               sf() const noexcept { return _cfg.sf; }
    [[nodiscard]] uint32_t              fft_size() const noexcept { return _N; }
    [[nodiscard]] std::span<const cf32> downchirp_ref() const noexcept { return {_cfo_downchirp.data(), _cfo_downchirp.size()}; }

private:
    /// Max-log LLR approximation. For each bit k, compute the log-likelihood
    /// ratio from max |Y|² over the subset of symbols whose k-th Gray-coded
    /// bit is 1, minus the same over bits-0 symbols.
    static void computeLlrInto(const float* mag_sq, uint32_t M, uint8_t sf_bits, const gr::lora::GrayPartition& gp, double* llr_out) {
        for (uint8_t k = 0; k < sf_bits; ++k) {
            float max_one = -std::numeric_limits<float>::infinity();
            for (uint32_t s : gp.ones[k]) {
                if (s < M) {
                    max_one = std::max(max_one, mag_sq[s]);
                }
            }
            float max_zero = -std::numeric_limits<float>::infinity();
            for (uint32_t s : gp.zeros[k]) {
                if (s < M) {
                    max_zero = std::max(max_zero, mag_sq[s]);
                }
            }
            llr_out[k] = static_cast<double>(max_one - max_zero);
        }
    }

    Config                  _cfg;
    uint32_t                _N = 0;
    std::vector<cf32>       _cfo_downchirp;
    std::vector<cf32>       _scratch;
    std::vector<float>      _mag_sq;
    std::vector<float>      _sym_mag_sq;
    gr::lora::GrayPartition _gp{};
};

} // namespace gr::lora::phy

#endif // GNURADIO_LORA_PHY_CSS_DEMOD_HPP
