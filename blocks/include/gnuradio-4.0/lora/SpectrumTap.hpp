// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_SPECTRUM_TAP_HPP
#define GNURADIO_LORA_SPECTRUM_TAP_HPP

#include <gnuradio-4.0/Block.hpp>
#include <gnuradio-4.0/DataSet.hpp>
#include <gnuradio-4.0/algorithm/fourier/fft.hpp>

#include <algorithm>
#include <cmath>
#include <complex>
#include <numbers>
#include <vector>

namespace gr::lora {

struct SpectrumTap : gr::Block<SpectrumTap, gr::NoTagPropagation> {
    using Description = Doc<"Passthrough cf32 block emitting periodic FFT energy reports on async port.">;

    gr::PortIn<std::complex<float>>            in;
    gr::PortOut<std::complex<float>>           out;
    gr::PortOut<gr::DataSet<float>, gr::Async> spectrum;

    float    sample_rate{16000000.f};
    float    channel_bw{62500.f};
    uint32_t fft_size{1024};
    uint32_t fft_accumulate{64};

    GR_MAKE_REFLECTABLE(SpectrumTap, in, out, spectrum, sample_rate, channel_bw, fft_size, fft_accumulate);

    gr::algorithm::FFT<std::complex<float>> _fft;
    std::vector<float>                      _window;
    std::vector<std::complex<float>>        _windowed;
    std::vector<float>                      _accumulator;
    std::vector<std::complex<float>>        _sampleBuffer;
    uint32_t                                _windowCount{0};
    bool                                    _spectrumReady{false};

    void start() {
        _window.resize(fft_size);
        for (uint32_t i = 0; i < fft_size; i++) {
            _window[i] = 0.5f * (1.f - std::cos(2.f * std::numbers::pi_v<float> * static_cast<float>(i) / static_cast<float>(fft_size)));
        }
        _windowed.resize(fft_size);
        _accumulator.assign(fft_size, 0.f);
        _sampleBuffer.clear();
        _sampleBuffer.reserve(fft_size);
        _windowCount   = 0;
        _spectrumReady = false;
        _fft           = gr::algorithm::FFT<std::complex<float>>{};
    }

    gr::work::Status processBulk(gr::InputSpanLike auto& inSpan, gr::OutputSpanLike auto& outSpan, gr::OutputSpanLike auto& spectrumSpan) noexcept {
        const auto inData  = std::span(inSpan);
        const auto outData = std::span(outSpan);
        const auto n       = std::min(inData.size(), outData.size());

        // Forward tags on the sync path.  Upstream PR #625 multi-tag API:
        // processBulk reads per-tag via inSpan.tags() instead of the
        // old mergedInputTag().  Publish each tag at its original
        // relative index so downstream sees the same per-sample timing.
        //
        // Must use `outSpan.publishTag(...)` (writes into the OutputSpan's
        // pre-reserved tags buffer) — NEVER `this->publishTag(...)` from
        // processBulk.  Upstream PR #439/#625 turned Writer into a
        // single-reserve-per-lifetime SingleWriter; Block::publishTag ->
        // Port::publishTag would call tagWriter().tryReserve(1) and trip
        // the CircularBuffer double-reserve abort (the OutputSpan ctor
        // already reserved the whole tag buffer).
        for (const auto& [relIndex, tagMapRef] : inSpan.tags()) {
            outSpan.publishTag(tagMapRef.get(), static_cast<std::size_t>(relIndex));
        }

        // passthrough: copy input to output
        std::copy_n(inData.begin(), n, outData.begin());

        // accumulate samples for FFT processing
        for (std::size_t offset = 0; offset < n;) {
            const auto need      = static_cast<std::size_t>(fft_size) - _sampleBuffer.size();
            const auto available = n - offset;
            const auto chunk     = std::min(need, available);

            _sampleBuffer.insert(_sampleBuffer.end(), inData.begin() + static_cast<std::ptrdiff_t>(offset), inData.begin() + static_cast<std::ptrdiff_t>(offset + chunk));
            offset += chunk;

            if (_sampleBuffer.size() == fft_size) {
                processWindow();
                _sampleBuffer.clear();
            }
        }

        // publish at most one spectrum report per processBulk call
        if (_spectrumReady) {
            publishSpectrum(spectrumSpan);
            _spectrumReady = false;
        }

        std::ignore = inSpan.consume(n);
        outSpan.publish(n);
        return gr::work::Status::OK;
    }

    void processWindow() noexcept {
        // apply Hann window
        for (uint32_t i = 0; i < fft_size; i++) {
            _windowed[i] = _sampleBuffer[i] * _window[i];
        }

        // compute FFT
        auto fftOut = _fft.compute(std::span<const std::complex<float>>(_windowed.data(), fft_size));

        // accumulate |FFT|² per bin (DC-centered via fftshift)
        const auto half = fft_size / 2;
        for (uint32_t i = 0; i < fft_size; i++) {
            const auto& s   = fftOut[(i + half) % fft_size];
            float       mag = s.real() * s.real() + s.imag() * s.imag();
            _accumulator[i] += mag;
        }
        _windowCount++;

        if (_windowCount >= fft_accumulate) {
            _spectrumReady = true;
            // accumulator holds the data; publishSpectrum reads it before reset
        }
    }

    void publishSpectrum(gr::OutputSpanLike auto& spectrumSpan) noexcept {
        auto outSpan = std::span(spectrumSpan);
        if (outSpan.empty()) {
            return; // async output buffer full, skip this report
        }

        gr::DataSet<float> ds;

        // axis: frequency (DC-centered)
        ds.axis_names.emplace_back("frequency");
        ds.axis_units.emplace_back("Hz");
        ds.axis_values.resize(1);
        ds.axis_values[0].resize(fft_size);
        const float binWidth = sample_rate / static_cast<float>(fft_size);
        for (uint32_t i = 0; i < fft_size; i++) {
            ds.axis_values[0][i] = (static_cast<float>(i) - static_cast<float>(fft_size / 2)) * binWidth;
        }

        // signal: energy in dBFS
        ds.signal_names.emplace_back("energy");
        ds.signal_units.emplace_back("dBFS");
        ds.signal_values.resize(fft_size);
        const float norm = 1.f / (static_cast<float>(fft_size) * static_cast<float>(fft_size) * static_cast<float>(_windowCount));
        for (uint32_t i = 0; i < fft_size; i++) {
            float avg           = _accumulator[i] * norm;
            ds.signal_values[i] = (avg > 1e-20f) ? 10.f * std::log10(avg) : -120.f;
        }

        ds.extents = {static_cast<std::int32_t>(fft_size)};

        outSpan[0] = std::move(ds);
        spectrumSpan.publish(1);

        std::ranges::fill(_accumulator, 0.f);
        _windowCount = 0;
    }
};

} // namespace gr::lora

#endif // GNURADIO_LORA_SPECTRUM_TAP_HPP
