// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_SCAN_CONTROLLER_HPP
#define GNURADIO_LORA_SCAN_CONTROLLER_HPP

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <numbers>
#include <numeric>
#include <ranges>
#include <span>
#include <vector>

#include <gnuradio-4.0/Block.hpp>
#include <gnuradio-4.0/DataSet.hpp>
#include <gnuradio-4.0/algorithm/fourier/fft.hpp>
#include <gnuradio-4.0/lora/ChannelActivityDetector.hpp>
#include <gnuradio-4.0/lora/algorithm/Channelize.hpp>
#include <gnuradio-4.0/lora/algorithm/L1Detector.hpp>
#include <gnuradio-4.0/lora/algorithm/RingBuffer.hpp>
#include <gnuradio-4.0/lora/log.hpp>

namespace gr::lora {

struct ScanController : gr::Block<ScanController, gr::NoTagPropagation> {
    using Description = Doc<"Wideband IQ sink with integrated L1 energy and L2 digital channelization.">;
    using cf32        = std::complex<float>;

    // --- ports ---
    gr::PortIn<cf32>                           in;
    gr::PortOut<gr::DataSet<float>, gr::Async> detect_out;
    gr::PortOut<gr::DataSet<float>, gr::Async> spectrum_out;

    // --- settings ---
    float       sample_rate{16000000.f};
    float       center_freq{866500000.f};
    float       min_ratio{8.0f};
    float       buffer_ms{512.f};
    float       channel_bw{62500.f};
    uint32_t    l1_interval{64};  // processBulk calls between L1 energy snapshots
    uint32_t    l1_snapshots{16}; // snapshots to accumulate before probing
    uint32_t    l1_fft_size{4096};
    std::string probe_bws{"62500"}; // comma-separated BWs to probe (Hz)

    GR_MAKE_REFLECTABLE(ScanController, in, detect_out, spectrum_out, sample_rate, center_freq, min_ratio, buffer_ms, channel_bw, l1_interval, l1_snapshots, l1_fft_size, probe_bws);

    // --- internal types ---

    enum class ScanState : uint8_t { Accumulate, Probe, Report };

    struct ProbeResult {
        double   freq{0.0};        // channel center frequency (Hz)
        float    ratio{0.f};       // CAD peak-to-median ratio
        float    chirp_slope{0.f}; // k = probe_bw² / 2^SF (Hz/s)
        float    probe_bw{0.f};    // probe bandwidth used (Hz, context only)
        uint32_t sf{0};            // detected spreading factor (7-12, 0 = unknown)
    };

    // --- internal state ---

    RingBuffer _ring;
    ScanState  _state{ScanState::Accumulate};
    uint32_t   _callCount{0};

    // L1 energy detection (FFT, Hann window, channel accumulation, hot detection,
    // per-channel peak-bin tracking for refinedFreq).
    L1Detector _l1;

    // Probe list for the current sweep. After the state machine transitions
    // from Accumulate to Probe, this holds the merged (rotating window +
    // L1-hot) channel list that probeStep iterates. It is NOT the raw
    // L1Detector output — that is copied in and then augmented below.
    std::vector<uint32_t>                 _hotChannels;
    std::vector<bool>                     _probeIsHot;    // true = L1-hot (all BWs narrowest-first)
    std::vector<bool>                     _probeIsRefine; // true = two-pass refinement (all BWs narrowest-first)
    std::vector<uint32_t>                 _pendingRefine; // channels queued for narrow-BW confirmation next sweep
    std::size_t                           _probeIndex{0};
    std::size_t                           _probeBwIndex{0};
    uint32_t                              _sweepCount{0};
    std::vector<ProbeResult>              _probeResults;
    ProbeResult                           _currentBest;
    std::vector<float>                    _bws;
    std::chrono::steady_clock::time_point _sweepStart{std::chrono::steady_clock::now()};
    uint32_t                              _probeWindowStart{0}; // rotating probe window position

    std::vector<ChannelActivityDetector> _cadPerBw;
    std::vector<CascadedDecimator>       _decimatorPerBw; // FIR channelizer per BW
    std::vector<cf32>                    _probeNbBuf;     // pre-allocated narrowband output
    std::vector<cf32>                    _probeWbBuf;     // pre-allocated wideband extract

    // Scan path has no per-sample DC blocker. L1 FFT NF interpolation +
    // DC-adjacent channel exclusion handle the spur. (DCBlocker is still
    // used by MultiSfDecoder for the narrowband decode path.)

    // --- lifecycle ---

    void start() {
        const auto bufferSamples = static_cast<std::size_t>(buffer_ms * sample_rate / 1000.f);
        _ring.resize(bufferSamples);

        // L1 channel grid + FFT + DC NF interpolation + hot detection
        L1Detector::Config l1cfg;
        l1cfg.sample_rate     = sample_rate;
        l1cfg.center_freq     = center_freq;
        l1cfg.channel_bw      = channel_bw;
        l1cfg.l1_fft_size     = l1_fft_size;
        l1cfg.usable_fraction = 0.8f;
        l1cfg.dc_null_hz      = 100'000.f;
        l1cfg.hot_multiplier  = 6.0f;
        l1cfg.min_hot_sweeps  = 1;
        // Truncate startBin (matches L1Detector default)
        _l1.init(l1cfg);

        // Parse configurable BWs from comma-separated string
        _bws.clear();
        {
            std::string s   = probe_bws;
            std::size_t pos = 0;
            while (pos < s.size()) {
                auto end = s.find(',', pos);
                if (end == std::string::npos) {
                    end = s.size();
                }
                if (end > pos) {
                    _bws.push_back(std::stof(s.substr(pos, end - pos)));
                }
                pos = end + 1;
            }
        }
        if (_bws.empty()) {
            _bws = {62500.f};
        }
        _cadPerBw.clear();
        _cadPerBw.reserve(_bws.size());
        _decimatorPerBw.clear();
        _decimatorPerBw.reserve(_bws.size());

        // Pre-compute the largest wideband sample count needed for L2 probing
        // (narrowest BW = highest decimation factor = most wideband samples)
        constexpr uint32_t kMaxSf       = 12;
        const uint32_t     cadSamples   = (1U << kMaxSf) * 2; // 8192
        uint32_t           maxWbSamples = 0;

        // CAD oversampling factor for timing search.  At os=1 the
        // peakRatio loop has a single timing offset (0), making SF7/SF8
        // windows (128/256 samples) very sensitive to where the chirp
        // lands in the buffer.  At os=2 the loop tries offsets {0, 1},
        // halving the worst-case timing loss and giving shorter SFs a
        // fair chance against longer SFs whose wider windows average
        // out timing misalignment.  Cost: one fewer decimation stage
        // (output at 2×BW instead of 1×BW) and 2× narrowband buffer.
        constexpr uint32_t kCadOs = 4U;

        for (float bw : _bws) {
            ChannelActivityDetector cad;
            cad.os_factor = kCadOs;
            cad.bandwidth = static_cast<uint32_t>(bw);
            cad.initMultiSf();
            _cadPerBw.push_back(std::move(cad));

            // CascadedDecimator: decimate to kCadOs × BW (not 1× BW).
            // Total wideband-to-BW ratio unchanged; one fewer stage so
            // output rate = 2×BW.
            const auto        osFactor = static_cast<uint32_t>(sample_rate / bw);
            const auto        nStages  = static_cast<std::size_t>(std::log2(osFactor / kCadOs));
            CascadedDecimator dec;
            dec.init(nStages, cadSamples * osFactor);
            _decimatorPerBw.push_back(std::move(dec));

            const uint32_t wbSamples = cadSamples * osFactor;
            maxWbSamples             = std::max(maxWbSamples, wbSamples);
        }

        // Pre-allocate probe buffers (avoid per-call heap allocations)
        _probeWbBuf.reserve(maxWbSamples);
        _probeNbBuf.reserve(cadSamples * kCadOs);

        _state      = ScanState::Accumulate;
        _callCount  = 0;
        _sweepCount = 0;

        gr::lora::log_ts("info ", "scanctrl", "started: %.1f MS/s, %.3f MHz center, %u ch (%.1f kHz), buffer %u ms, L1 every %u calls", static_cast<double>(sample_rate) / 1e6, static_cast<double>(center_freq) / 1e6, _l1.nChannels(), static_cast<double>(channel_bw) / 1e3, static_cast<unsigned>(buffer_ms), l1_interval);
    }

    // --- processBulk ---

    gr::work::Status processBulk(gr::InputSpanLike auto& inPort, [[maybe_unused]] gr::OutputSpanLike auto& detectOutPort, gr::OutputSpanLike auto& spectrumOutPort) noexcept {
        // 1. Check overflow tags (upstream PR #625 multi-tag API: processBulk
        // reads per-tag via inPort.tags() instead of mergedInputTag())
        for (const auto& [relIndex, tagMapRef] : inPort.tags()) {
            if (tagMapRef.get().contains("overflow")) {
                _ring.reset();
                _state = ScanState::Accumulate;
                _l1.resetSweep();
                // Discard any pending two-pass refinements — the channel
                // state they were based on is stale after an overflow.
                _pendingRefine.clear();
                break;
            }
        }

        // 2. Push raw samples into ring buffer (no DC blocker).
        // The DC spur is handled by two separate mechanisms:
        //   - L1 energy: FFT NF interpolation replaces DC bins with noise floor
        //   - L2 probes: DC-adjacent channels (±625 kHz) are excluded from probing
        auto inSpan = std::span(inPort);
        _ring.push(inSpan);
        std::ignore = inPort.consume(inSpan.size());

        // 3. Periodic L1 energy snapshot (cheap: one FFT per interval)
        ++_callCount;
        if (_state == ScanState::Accumulate && _callCount >= l1_interval) {
            _callCount = 0;
            if (_ring.count >= l1_fft_size) {
                const auto samples = _ring.recent(l1_fft_size);
                if (!samples.empty()) {
                    _l1.accumulate(std::span<const cf32>(samples.data(), samples.size()));
                }
            }
        }

        // 4. State machine
        std::size_t spectrumPublished = 0;

        switch (_state) {
        case ScanState::Accumulate:
            if (_l1.snapshotCount() >= l1_snapshots) {
                // Copy raw L1 hot channels into block storage; the list is
                // augmented below with rotating-window entries for probeStep.
                auto rawHot = _l1.findHotChannels();
                _hotChannels.assign(rawHot.begin(), rawHot.end());

                // L1 FFT energy gating cannot detect BW62.5k chirp signals at
                // 16 MS/s (energy dilutes to ~1× median across FFT bins).
                // Use a rotating probe window: probe kProbeWindowSize channels
                // per sweep, cycling through the full band. L1 hot channels
                // are merged in so energy-detected signals still get priority.
                constexpr uint32_t kProbeWindowSize = 8;

                // Only probe channels within the usable bandwidth (80% of
                // sample_rate).  Band-edge channels beyond this have severe
                // anti-aliasing filter roll-off and never contain real signals.
                // Also skip DC-adjacent channels where the ADC DC spur cluster
                // (up to ±500 kHz skirt on AD9361) produces false hot channels.
                const uint32_t  centerCh        = _l1.nChannels() / 2;
                constexpr float kDcExcludeHz    = 625000.f; // ±625 kHz DC spur skirt (10 ch at 62.5 kHz)
                constexpr float kUsableFrac     = 0.70f;    // conservative usable fraction
                const uint32_t  dcExcludeRadius = static_cast<uint32_t>(kDcExcludeHz / channel_bw) + 1;
                const uint32_t  usableRadius    = static_cast<uint32_t>(sample_rate * kUsableFrac / (2.f * channel_bw));
                const uint32_t  usableStart     = (centerCh > usableRadius) ? centerCh - usableRadius : 0;
                const uint32_t  usableEnd       = std::min(centerCh + usableRadius, _l1.nChannels());

                // Advance rotating window, skipping DC-excluded and band-edge channels
                if (_probeWindowStart < usableStart || _probeWindowStart >= usableEnd) {
                    _probeWindowStart = usableStart;
                }
                // Skip past DC-excluded zone
                uint32_t wStart = _probeWindowStart;
                if (wStart >= centerCh - dcExcludeRadius && wStart <= centerCh + dcExcludeRadius) {
                    wStart = centerCh + dcExcludeRadius + 1;
                }
                const uint32_t windowEnd = std::min(wStart + kProbeWindowSize, usableEnd);

                // Build probe list: rotating window + L1 hot + pending-refine.
                // Rotating channels get probed at widest BW only (cheapest).
                // L1-hot channels get probed at all BWs (narrowest first).
                // Pending-refine channels hit at the widest BW in a previous
                // sweep and are re-probed narrowest-first here to resolve the
                // chirp-slope SF alias (k = BW^2 / 2^SF is degenerate across
                // (SF, BW) pairs — narrowest BW gives the canonical SF).
                std::vector<uint32_t> probeChannels;
                std::vector<bool>     probeHot;
                std::vector<bool>     probeRefine;
                for (uint32_t ch = wStart; ch < windowEnd; ++ch) {
                    // Skip DC-excluded channels in the window
                    const auto dist = (ch > centerCh) ? ch - centerCh : centerCh - ch;
                    if (dist <= dcExcludeRadius) {
                        continue;
                    }
                    probeChannels.push_back(ch);
                    probeHot.push_back(false); // rotating window = widest BW only
                    probeRefine.push_back(false);
                }
                // Merge L1 hot channels not already in the window,
                // excluding DC-adjacent channels (persistent ADC DC spur)
                for (uint32_t ch : _hotChannels) {
                    if (ch < wStart || ch >= windowEnd) {
                        const auto dist = (ch > centerCh) ? ch - centerCh : centerCh - ch;
                        if (dist <= dcExcludeRadius) {
                            continue;
                        }
                        if (ch < usableStart || ch >= usableEnd) {
                            continue;
                        }
                        probeChannels.push_back(ch);
                        probeHot.push_back(true); // L1-hot = all BWs
                        probeRefine.push_back(false);
                    }
                }
                // Merge pending-refine channels (queued from a prior sweep's
                // widest-BW rotating hit).  Re-probed narrowest-first to get
                // the correct SF.  May duplicate a rotating-window channel
                // when the window wraps; harmless — a duplicate just probes
                // twice this sweep (still narrowest-first-dominant).
                for (uint32_t ch : _pendingRefine) {
                    const auto dist = (ch > centerCh) ? ch - centerCh : centerCh - ch;
                    if (dist <= dcExcludeRadius) {
                        continue;
                    }
                    if (ch < usableStart || ch >= usableEnd) {
                        continue;
                    }
                    probeChannels.push_back(ch);
                    probeHot.push_back(false);
                    probeRefine.push_back(true); // two-pass refinement
                }
                _pendingRefine.clear();

                // Advance window for next sweep (wrap within usable range)
                _probeWindowStart = (windowEnd >= usableEnd) ? usableStart : windowEnd;

                _hotChannels   = std::move(probeChannels);
                _probeIsHot    = std::move(probeHot);
                _probeIsRefine = std::move(probeRefine);
                _probeIndex    = 0;
                _probeBwIndex  = 0;
                _probeResults.clear();
                _state = ScanState::Probe;
            }
            break;

        case ScanState::Probe: {
            bool done = probeStep();
            if (done) {
                _state = ScanState::Report;
            }
            break;
        }

        case ScanState::Report:
            spectrumPublished = emitSpectrum(spectrumOutPort);
            if (spectrumPublished > 0) {
                _sweepCount++;
                resetAccumulation();
                _state = ScanState::Accumulate;
            }
            // else: output full, retry next processBulk call
            break;
        }

        spectrumOutPort.publish(spectrumPublished);
        return gr::work::Status::OK;
    }

    // Test-only wrappers — production processBulk calls _l1 directly.
    void computeEnergySnapshot() noexcept {
        if (_ring.count < l1_fft_size) {
            return;
        }
        const auto recent = _ring.recent(l1_fft_size);
        if (recent.empty()) {
            return;
        }
        _l1.accumulate(std::span<const cf32>(recent.data(), recent.size()));
    }

    void findHotChannels() noexcept {
        const auto raw = _l1.findHotChannels();
        _hotChannels.assign(raw.begin(), raw.end());
    }

    // --- L2 probe (one BW per call) ---

    bool probeStep() noexcept {
        if (_probeIndex >= _hotChannels.size()) {
            return true;
        }

        const uint32_t ch          = _hotChannels[_probeIndex];
        const double   channelFreq = channelCenterFreq(ch);

        // Initialize best result for this channel on first BW.
        // All channels probed narrowest-first (correct SF identification).
        // Narrowest BW resolves the chirp-slope SF alias (k = BW²/2^SF)
        // and stops early once a detection exceeds min_ratio.
        if (_probeBwIndex == 0) {
            _currentBest      = ProbeResult{};
            _currentBest.freq = channelFreq;
        }

        if (_probeBwIndex < _bws.size() && _currentBest.ratio < min_ratio) {
            // Probe narrowest BW first; stop as soon as one detects above threshold.
            // Narrowest BW gives correct SF identification (wider BWs detect
            // chirp-slope equivalents at different SFs due to k = BW²/2^SF).
            const float probeBw = _bws[_probeBwIndex];
            auto&       cad     = _cadPerBw[_probeBwIndex];
            auto&       dec     = _decimatorPerBw[_probeBwIndex];

            constexpr uint32_t kMaxSf     = 12;
            const uint32_t     cadSamples = (1U << kMaxSf) * 2;
            const auto         osFactor   = static_cast<uint32_t>(sample_rate / probeBw);
            const uint32_t     wbSamples  = cadSamples * osFactor;

            // Extract wideband IQ from ring buffer into pre-allocated buffer
            if (_ring.count < wbSamples) {
                ++_probeBwIndex;
                return false;
            }
            auto wbView = _ring.recentView(wbSamples);
            if (wbView.first.size() + wbView.second.size() >= wbSamples) {
                // Linearize the two spans into _probeWbBuf (ring may wrap)
                _probeWbBuf.resize(wbSamples);
                std::copy_n(wbView.first.data(), wbView.first.size(), _probeWbBuf.data());
                if (!wbView.second.empty()) {
                    std::copy_n(wbView.second.data(), wbView.second.size(), _probeWbBuf.data() + wbView.first.size());
                }

                // Channelize: fused NCO + cascaded half-band FIR (-45 dB stopband)
                channelizeFir(_probeWbBuf.data(), wbSamples, channelFreq, static_cast<double>(center_freq), static_cast<double>(sample_rate), dec, _probeNbBuf);

                if (_probeNbBuf.size() >= cadSamples) {
                    // OR mode (require_both=false): scan probes are
                    // isolated channel extracts, not continuous streams.
                    // One good 1-symbol window suffices for detection.
                    // Combined with lowest-SF-wins in detectMultiSf,
                    // this gives correct SF classification at marginal
                    // SNR where AND mode fails the correct SF but passes
                    // a higher SF due to FFT coherent-gain scaling.
                    auto det = cad.detectMultiSf(_probeNbBuf.data(),
                        /*require_both=*/false);
                    if (det.detected && det.best_ratio > _currentBest.ratio) {
                        _currentBest.ratio       = det.best_ratio;
                        _currentBest.probe_bw    = probeBw;
                        _currentBest.sf          = det.sf;
                        _currentBest.chirp_slope = probeBw * probeBw / static_cast<float>(1U << det.sf);
                    }
                }
            }

            ++_probeBwIndex;
        }

        // Done if: all BWs tried, or detection above threshold
        if (_probeBwIndex >= _bws.size() || _currentBest.ratio >= min_ratio) {
            if (_currentBest.ratio >= min_ratio) {
                _probeResults.push_back(_currentBest);
            }
            ++_probeIndex;
            _probeBwIndex = 0;
        }

        return _probeIndex >= _hotChannels.size();
    }

    // --- output ---

    [[nodiscard]] std::size_t emitSpectrum(gr::OutputSpanLike auto& outPort) noexcept {
        auto           outSpan = std::span(outPort);
        const uint32_t nCh     = _l1.nChannels();
        if (outSpan.empty() || nCh == 0) {
            return 0;
        }

        gr::DataSet<float> ds;
        ds.timestamp = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch()).count();

        ds.signal_names.emplace_back("channel_energy");
        ds.signal_units.emplace_back("linear");
        ds.extents = {static_cast<int32_t>(nCh)};

        const auto  snapshots     = _l1.snapshotCount();
        const float scale         = (snapshots > 0) ? 1.0f / static_cast<float>(snapshots) : 1.0f;
        const auto  channelEnergy = _l1.channelEnergy();
        ds.signal_values.resize(nCh);
        for (uint32_t i = 0; i < nCh; ++i) {
            ds.signal_values[i] = channelEnergy[i] * scale;
        }

        ds.axis_names.emplace_back("frequency");
        ds.axis_units.emplace_back("Hz");
        ds.axis_values.resize(1);
        ds.axis_values[0].resize(nCh);
        for (uint32_t i = 0; i < nCh; ++i) {
            ds.axis_values[0][i] = static_cast<float>(channelCenterFreq(i));
        }

        ds.meta_information.resize(1);
        const auto sweepDur = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - _sweepStart).count();

        ds.meta_information[0]["type"]        = std::string("scan_spectrum");
        ds.meta_information[0]["sweep"]       = static_cast<int64_t>(_sweepCount);
        ds.meta_information[0]["n_hot"]       = static_cast<int64_t>(_hotChannels.size());
        ds.meta_information[0]["n_snapshots"] = static_cast<int64_t>(snapshots);
        ds.meta_information[0]["n_det"]       = static_cast<int64_t>(_probeResults.size());
        ds.meta_information[0]["duration_ms"] = static_cast<int64_t>(sweepDur);

        // Embed hot channel indices as comma-separated string
        // (pmt::Value doesn't support vector<int64_t>, so we serialize)
        {
            std::string hotStr;
            for (std::size_t i = 0; i < _hotChannels.size(); ++i) {
                if (i > 0) {
                    hotStr += ',';
                }
                hotStr += std::to_string(_hotChannels[i]);
            }
            ds.meta_information[0]["hot_indices"] = std::move(hotStr);
        }

        // Embed detection results as indexed metadata fields
        for (std::size_t i = 0; i < _probeResults.size(); ++i) {
            const auto& r                                                          = _probeResults[i];
            const auto  idx                                                        = std::to_string(i);
            ds.meta_information[0][std::pmr::string("det" + idx + "_freq")]        = r.freq;
            ds.meta_information[0][std::pmr::string("det" + idx + "_ratio")]       = static_cast<float>(r.ratio);
            ds.meta_information[0][std::pmr::string("det" + idx + "_chirp_slope")] = r.chirp_slope;
            ds.meta_information[0][std::pmr::string("det" + idx + "_probe_bw")]    = r.probe_bw;
            ds.meta_information[0][std::pmr::string("det" + idx + "_sf")]          = static_cast<int64_t>(r.sf);
        }

        outSpan[0] = std::move(ds);
        return 1;
    }

    void resetAccumulation() noexcept {
        _l1.resetSweep();
        _callCount = 0;
        _hotChannels.clear();
        _sweepStart = std::chrono::steady_clock::now();
    }

    // Refined frequency from L1 FFT peak bin (~3.9 kHz resolution vs 62.5 kHz
    // channel center). Thin wrapper that delegates to L1Detector.
    [[nodiscard]] double refinedFreq(uint32_t ch) const noexcept { return _l1.refinedFreq(ch); }

    [[nodiscard]] double channelCenterFreq(uint32_t ch) const noexcept { return _l1.channelCenterFreq(ch); }
};

} // namespace gr::lora

#endif // GNURADIO_LORA_SCAN_CONTROLLER_HPP
