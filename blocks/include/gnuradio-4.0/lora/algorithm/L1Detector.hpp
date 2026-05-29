// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_ALGORITHM_L1_DETECTOR_HPP
#define GNURADIO_LORA_ALGORITHM_L1_DETECTOR_HPP

// L1Detector: shared L1 energy detection primitives used by
// ScanController. Header-only helper, not a GR4 Block.
//
// Responsibilities:
//   - windowed L1 FFT over an input span of exactly l1_fft_size samples
//   - DC bin null-interpolation (replace DC spur region with NF estimate)
//   - per-channel energy accumulation in fftshifted domain
//   - per-channel peak-bin tracking (for frequency refinement)
//   - hot channel detection via median threshold + persistence + cluster dedup

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <numbers>
#include <ranges>
#include <span>
#include <vector>

#include <gnuradio-4.0/algorithm/fourier/fft.hpp>

namespace gr::lora {

struct L1Detector {
    using cf32 = std::complex<float>;

    struct Config {
        float    sample_rate     = 16'000'000.f;
        float    center_freq     = 866'500'000.f;
        float    channel_bw      = 62'500.f;
        uint32_t l1_fft_size     = 4096;
        float    usable_fraction = 0.8f;
        float    dc_null_hz      = 100'000.f;
        float    hot_multiplier  = 6.0f;
        uint32_t min_hot_sweeps  = 1;
    };

    Config                   _cfg{};
    gr::algorithm::FFT<cf32> _fft{};
    std::vector<float>       _window{};
    std::vector<cf32>        _fftBuf{};
    std::vector<float>       _channelEnergy{};
    std::vector<uint32_t>    _channelHotCount{};
    std::vector<uint32_t>    _channelPeakBin{};
    std::vector<float>       _channelPeakMag{};
    std::vector<uint32_t>    _hotChannels{};
    uint32_t                 _nChannels{0};
    uint32_t                 _snapshotCount{0};

    void init(const Config& cfg) {
        _cfg = cfg;

        const float usableBw = _cfg.sample_rate * _cfg.usable_fraction;
        _nChannels           = static_cast<uint32_t>(usableBw / _cfg.channel_bw);

        _channelEnergy.assign(_nChannels, 0.f);
        _channelHotCount.assign(_nChannels, 0);
        _channelPeakBin.assign(_nChannels, 0);
        _channelPeakMag.assign(_nChannels, 0.f);

        _window.resize(_cfg.l1_fft_size);
        for (uint32_t i = 0; i < _cfg.l1_fft_size; ++i) {
            _window[i] = 0.5f * (1.f - std::cos(2.f * std::numbers::pi_v<float> * static_cast<float>(i) / static_cast<float>(_cfg.l1_fft_size)));
        }
        _fft = gr::algorithm::FFT<cf32>{};
        _fftBuf.reserve(_cfg.l1_fft_size);

        _snapshotCount = 0;
        _hotChannels.clear();
    }

    void resetSweep() noexcept {
        std::ranges::fill(_channelEnergy, 0.f);
        std::ranges::fill(_channelPeakBin, 0u);
        std::ranges::fill(_channelPeakMag, 0.f);
        _snapshotCount = 0;
        _hotChannels.clear();
    }

    // Accumulate one snapshot from a contiguous span. If the span has fewer
    // than l1_fft_size samples, the call is silently ignored and snapshotCount
    // is not incremented. The last l1_fft_size samples of the span are used.
    void accumulate(std::span<const cf32> samples) noexcept {
        if (samples.size() < _cfg.l1_fft_size) {
            return;
        }

        const auto src = samples.subspan(samples.size() - _cfg.l1_fft_size, _cfg.l1_fft_size);
        _fftBuf.resize(_cfg.l1_fft_size);
        std::copy(src.begin(), src.end(), _fftBuf.begin());

        for (uint32_t i = 0; i < _cfg.l1_fft_size; ++i) {
            _fftBuf[i] *= _window[i];
        }

        auto fftOut = _fft.compute(std::span<const cf32>(_fftBuf.data(), _cfg.l1_fft_size));

        interpolateDcNullBins(std::span<cf32>(fftOut.data(), fftOut.size()));
        accumulateChannelEnergy(std::span<const cf32>(fftOut.data(), fftOut.size()));

        ++_snapshotCount;
    }

    // Compute and return the sorted, cluster-dedup'd hot channel indices.
    // The returned span references internal storage and is valid until the
    // next call to accumulate, resetSweep, or findHotChannels. The caller is
    // responsible for applying any DC-exclude filter.
    [[nodiscard]] std::span<const uint32_t> findHotChannels() noexcept {
        _hotChannels.clear();
        if (_nChannels == 0 || _snapshotCount == 0) {
            return {};
        }

        std::vector<float> sorted(_channelEnergy.begin(), _channelEnergy.begin() + static_cast<std::ptrdiff_t>(_nChannels));
        std::ranges::sort(sorted);
        const float median    = sorted[sorted.size() / 2];
        const float threshold = median * _cfg.hot_multiplier;

        for (uint32_t ch = 0; ch < _nChannels; ++ch) {
            if (_channelEnergy[ch] > threshold) {
                ++_channelHotCount[ch];
            } else {
                _channelHotCount[ch] = 0;
            }
        }

        std::vector<uint32_t> raw;
        raw.reserve(_nChannels);
        for (uint32_t ch = 0; ch < _nChannels; ++ch) {
            if (_channelHotCount[ch] >= _cfg.min_hot_sweeps) {
                raw.push_back(ch);
            }
        }

        std::size_t i = 0;
        while (i < raw.size()) {
            uint32_t    bestCh = raw[i];
            float       bestE  = _channelEnergy[bestCh];
            std::size_t j      = i + 1;
            while (j < raw.size() && raw[j] == raw[j - 1] + 1) {
                if (_channelEnergy[raw[j]] > bestE) {
                    bestCh = raw[j];
                    bestE  = _channelEnergy[raw[j]];
                }
                ++j;
            }
            _hotChannels.push_back(bestCh);
            i = j;
        }

        return {_hotChannels.data(), _hotChannels.size()};
    }

    [[nodiscard]] uint32_t nChannels() const noexcept { return _nChannels; }
    [[nodiscard]] uint32_t snapshotCount() const noexcept { return _snapshotCount; }

    [[nodiscard]] std::span<const float>    channelEnergy() const noexcept { return {_channelEnergy.data(), _channelEnergy.size()}; }
    [[nodiscard]] std::span<const uint32_t> channelPeakBin() const noexcept { return {_channelPeakBin.data(), _channelPeakBin.size()}; }
    [[nodiscard]] std::span<const float>    channelPeakMag() const noexcept { return {_channelPeakMag.data(), _channelPeakMag.size()}; }

    // Hz-based channel center frequency.
    [[nodiscard]] double channelCenterFreq(uint32_t ch) const noexcept {
        const double usableBw  = static_cast<double>(_cfg.sample_rate) * static_cast<double>(_cfg.usable_fraction);
        const double bandStart = static_cast<double>(_cfg.center_freq) - usableBw / 2.0;
        return bandStart + (static_cast<double>(ch) + 0.5) * static_cast<double>(_cfg.channel_bw);
    }

    // Refined channel frequency using the per-channel FFT peak bin.
    // Gives ~binHz resolution vs ~channel_bw resolution of channelCenterFreq.
    [[nodiscard]] double refinedFreq(uint32_t ch) const noexcept {
        if (ch < _nChannels && _channelPeakMag[ch] > 0.f) {
            const double binBw     = static_cast<double>(_cfg.sample_rate) / static_cast<double>(_cfg.l1_fft_size);
            const double usableBw  = static_cast<double>(_cfg.sample_rate) * static_cast<double>(_cfg.usable_fraction);
            const double bandStart = static_cast<double>(_cfg.center_freq) - usableBw / 2.0;
            return bandStart + static_cast<double>(_channelPeakBin[ch]) * binBw + binBw * 0.5;
        }
        return channelCenterFreq(ch);
    }

private:
    void interpolateDcNullBins(std::span<cf32> fftOut) noexcept {
        // Replace DC spur bins with a noise-floor estimate. The B210 AD9361 DC
        // offset at 16 MS/s sits ~22 dB above NF — no IIR HP filter can suppress
        // it sufficiently, so we interpolate NF into the DC region.
        const float binHz         = _cfg.sample_rate / static_cast<float>(_cfg.l1_fft_size);
        const auto  kDcNullRadius = std::min(static_cast<uint32_t>(_cfg.dc_null_hz / binHz), _cfg.l1_fft_size / 8);
        const auto  kRefBins      = std::max(kDcNullRadius, 8u);

        float refMagSq = 0.f;
        for (uint32_t b = kDcNullRadius + 1; b <= kDcNullRadius + kRefBins && b < _cfg.l1_fft_size / 2; ++b) {
            const auto& s1 = fftOut[b];
            refMagSq += s1.real() * s1.real() + s1.imag() * s1.imag();
            const auto& s2 = fftOut[_cfg.l1_fft_size - b];
            refMagSq += s2.real() * s2.real() + s2.imag() * s2.imag();
        }
        const float nfMag = std::sqrt(refMagSq / static_cast<float>(2 * kRefBins));
        for (uint32_t b = 0; b <= kDcNullRadius; ++b) {
            fftOut[b] = {nfMag, 0.f};
            if (b > 0) {
                fftOut[_cfg.l1_fft_size - b] = {nfMag, 0.f};
            }
        }
    }

    void accumulateChannelEnergy(std::span<const cf32> fftOut) noexcept {
        const auto  half      = _cfg.l1_fft_size / 2;
        const float binBw     = _cfg.sample_rate / static_cast<float>(_cfg.l1_fft_size);
        const auto  binsPerCh = static_cast<uint32_t>(_cfg.channel_bw / binBw);

        const float usableBw = _cfg.sample_rate * _cfg.usable_fraction;
        const float rawStart = (static_cast<float>(_cfg.l1_fft_size) - usableBw / binBw) / 2.f;
        const auto  startBin = static_cast<uint32_t>(rawStart);

        for (uint32_t ch = 0; ch < _nChannels && ch * binsPerCh + startBin + binsPerCh <= _cfg.l1_fft_size; ++ch) {
            float energy = 0.f;
            for (uint32_t b = 0; b < binsPerCh; ++b) {
                const auto  fftIdx = (startBin + ch * binsPerCh + b + half) % _cfg.l1_fft_size;
                const auto& s      = fftOut[fftIdx];
                const float mag    = s.real() * s.real() + s.imag() * s.imag();
                energy += mag;
                if (mag > _channelPeakMag[ch]) {
                    _channelPeakMag[ch] = mag;
                    _channelPeakBin[ch] = startBin + ch * binsPerCh + b;
                }
            }
            _channelEnergy[ch] += energy;
        }
    }
};

} // namespace gr::lora

#endif // GNURADIO_LORA_ALGORITHM_L1_DETECTOR_HPP
