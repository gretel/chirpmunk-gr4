// SPDX-License-Identifier: ISC
//
// lora_scan — LoRa spectral scanner (GR4 graph + orchestrator)
//
// A GR4 flowgraph handles continuous streaming from the SDR with proper
// backpressure, while an orchestrator thread drives the scan logic:
//   Layer 1: wideband FFT energy scan → candidate channels
//   Layer 2: per-channel CAD dwell with shared-buffer-per-BW detection
//
// Graph topology (tuning mode):
//   SoapySimpleSource(L1 rate) → Splitter(2)
//       → [0] SpectrumTapBlock → NullSink    (L1 energy via SpectrumState)
//       → [1] CaptureSink                     (L2 CAD on-demand capture)
//
// Configuration via TOML (shared with lora_trx).
// Run `lora_scan --help` for usage.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
#include <mutex>
#include <numeric>
#include <string>
#include <thread>
#include <vector>

#include <unistd.h>

#include <gnuradio-4.0/Scheduler.hpp>
#include <gnuradio-4.0/lora/ChannelActivityDetector.hpp>
#include <gnuradio-4.0/lora/algorithm/PreambleId.hpp>

#include "graph_builder.hpp"

#include <gnuradio-4.0/lora/algorithm/Telemetry.hpp>
#include <gnuradio-4.0/lora/cbor.hpp>
#include <gnuradio-4.0/lora/log.hpp>

#include "udp_state.hpp"

#ifndef GIT_REV
#define GIT_REV "dev"
#endif

using lora_apps::cf32;
using lora_apps::UdpState;
using lora_config::ScanSetConfig;
using lora_graph::ScanGraph;

struct ScanStats {
    uint32_t    sweeps           = 0;
    uint32_t    overflows        = 0;
    uint32_t    drops            = 0;
    uint32_t    l1_hot           = 0;
    uint32_t    total_detections = 0;
    std::string last_det_freq;
    std::string last_det_time;
};

// ─── CLI ──────────────────────────────────────────────────────────────────────

static int parse_scan_args(int argc, char** argv, std::string& config_path, std::string& log_level, bool& cbor_out) {
    config_path = "apps/config.toml";
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--config" && i + 1 < argc) {
            config_path = argv[++i];
            continue;
        }
        if (arg == "--cbor") {
            cbor_out = true;
            continue;
        }
        if (arg == "--log-level" && i + 1 < argc) {
            log_level = argv[++i];
            for (auto& ch : log_level) {
                ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
            }
            continue;
        }
        if (arg == "--version") {
            std::fprintf(stderr, "lora_scan %s\n", GIT_REV);
            return 2;
        }
        if (arg == "-h" || arg == "--help") {
            std::fprintf(stderr, "Usage: lora_scan [options]\n\n"
                                 "LoRa spectral scanner: sweep channels and detect chirp activity.\n\n"
                                 "Options:\n"
                                 "  --config <file>       TOML configuration file (default: apps/config.toml)\n"
                                 "  --cbor                Emit CBOR frames on stdout (pipe to lora_spectrum.py)\n"
                                 "  --log-level <level>   Log level: DEBUG, INFO, WARNING, ERROR\n"
                                 "  --version             Show version and exit\n"
                                 "  -h, --help            Show this help\n");
            return 2;
        }
        std::fprintf(stderr, "Unknown argument: %s\n", arg.c_str());
        return 1;
    }
    return 0;
}

// ─── channel list ─────────────────────────────────────────────────────────────

static std::vector<double> makeChannelList(double freqStart, double freqStop, double bw) {
    std::vector<double> channels;
    for (double freq = freqStart; freq <= freqStop + bw * 0.01; freq += bw) {
        channels.push_back(freq);
    }
    return channels;
}

// ─── Orchestrator helpers ────────────────────────────────────────────────────

/// Retune the SoapySource frequency via GR4 settings (no stream restart).
/// The graph keeps streaming during the settle delay -- no overflow.
static void retune_source(std::shared_ptr<gr::BlockModel>& source, double freq, int settleMs) {
    gr::property_map props;
    props["frequency"] = std::vector<double>{freq};
    std::ignore        = source->settings().set(props);
    std::this_thread::sleep_for(std::chrono::milliseconds(settleMs));
}

/// Update the SpectrumState centre frequency to match a retune.
static void update_spectrum_centre(std::shared_ptr<gr::lora::SpectrumState>& spectrum, double freq) { spectrum->center_freq = freq; }

/// Trigger an on-demand capture via CaptureSink and wait for completion.
/// Returns a span over the captured data, or empty span on stop.
static std::span<const cf32> capture_samples(std::shared_ptr<gr::lora::CaptureState>& state, uint32_t nSamples, const std::atomic<bool>& stopFlag, uint32_t timeout_ms = 2000) {
    state->request(nSamples);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (!state->done.load(std::memory_order_acquire)) {
        if (stopFlag.load(std::memory_order_relaxed)) {
            state->reset();
            return {};
        }
        if (std::chrono::steady_clock::now() > deadline) {
            auto captured = state->captured.load(std::memory_order_relaxed);
            std::fprintf(stderr, "capture timeout: got %u/%u samples\n", captured, nSamples);
            state->reset();
            return {};
        }
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    state->reset();
    return {state->buffer.data(), nSamples};
}

/// Decimate a wideband (L1-rate) capture to a narrowband target rate.
/// Uses a simple integrate-and-dump (box-car average) decimation filter.
/// Requires l1Rate / targetRate to be an integer.  Returns decimated buffer.
static std::vector<cf32> decimate(const cf32* src, uint32_t nSamples, double l1Rate, double targetRate) {
    const auto factor = static_cast<uint32_t>(std::round(l1Rate / targetRate));
    if (factor <= 1) {
        return std::vector<cf32>(src, src + nSamples);
    }
    const uint32_t    outLen = nSamples / factor;
    std::vector<cf32> out(outLen);
    const float       scale = 1.0F / static_cast<float>(factor);
    for (uint32_t i = 0; i < outLen; ++i) {
        cf32 sum{0.0F, 0.0F};
        for (uint32_t j = 0; j < factor; ++j) {
            sum += src[i * factor + j];
        }
        out[i] = sum * scale;
    }
    return out;
}

// ─── Layer 1: wideband FFT energy scan via SpectrumState ─────────────────────

struct Layer1Result {
    std::vector<double> energy;
    std::vector<int>    hot_idx;
};

/// Extract per-channel energy from a single SpectrumState FFT result.
/// The spectrum must already be tuned to the tile centre and have fresh data.
static bool extractTileEnergy(std::shared_ptr<gr::lora::SpectrumState>& spectrum, const std::vector<double>& channels, double chBw, std::vector<double>& energy, uint32_t timeout_ms = 500) {
    // Poll until a fresh FFT result is available (with timeout)
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (!spectrum->compute()) {
        if (std::chrono::steady_clock::now() > deadline) {
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    // Copy magnitude_db under lock
    std::vector<float> mag_db;
    {
        std::lock_guard lock(spectrum->result_mutex);
        mag_db = spectrum->magnitude_db;
    }

    const auto   fftSize    = spectrum->fft_size;
    const double sampleRate = static_cast<double>(spectrum->sample_rate);
    const double centerFreq = spectrum->center_freq;
    const double hzPerBin   = sampleRate / static_cast<double>(fftSize);
    const double halfTile   = sampleRate / 2.0;

    for (std::size_t chIdx = 0; chIdx < channels.size(); ++chIdx) {
        const double chOffset = channels[chIdx] - centerFreq;
        if (std::abs(chOffset) > halfTile * 0.9) {
            continue;
        }

        // DC-centered FFT: bin 0 = -fs/2, bin fftSize/2 = DC
        const double binCentre  = static_cast<double>(fftSize) / 2.0 + chOffset / hzPerBin;
        const double halfChBins = (chBw / sampleRate) * static_cast<double>(fftSize) / 2.0;
        const int    kLo        = static_cast<int>(std::round(binCentre - halfChBins));
        const int    kHi        = static_cast<int>(std::round(binCentre + halfChBins));

        double sum = 0.0;
        int    cnt = 0;
        for (int k = kLo; k <= kHi; ++k) {
            if (k < 0 || k >= static_cast<int>(fftSize)) {
                continue;
            }
            // Convert dBFS back to linear power for summation
            sum += std::pow(10.0, static_cast<double>(mag_db[static_cast<std::size_t>(k)]) / 10.0);
            ++cnt;
        }
        if (cnt > 0) {
            const double snapEnergy = sum / static_cast<double>(cnt);
            energy[chIdx]           = std::max(energy[chIdx], snapEnergy);
        }
    }
    return true;
}

/// L1 scan using SpectrumState.  For single-tile configs, reads the spectrum
/// directly.  For multi-tile, retunes the SoapySource to each tile centre.
static Layer1Result layer1Scan(std::shared_ptr<gr::lora::SpectrumState>& spectrum, std::shared_ptr<gr::BlockModel>& soapy_source, const std::vector<double>& channels, const ScanSetConfig& cfg) {
    const std::size_t nCh = channels.size();
    Layer1Result      result;
    result.energy.resize(nCh, 0.0);

    const double chBw      = cfg.min_bw();
    const double scanRange = channels.back() - channels.front() + chBw;
    const double usableBw  = cfg.l1_rate * 0.8;

    // Take multiple FFT snapshots per tile for stability
    constexpr int kL1Snapshots = 4;

    if (scanRange <= usableBw) {
        // Single tile: spectrum is already centred
        for (int snap = 0; snap < kL1Snapshots; ++snap) {
            extractTileEnergy(spectrum, channels, chBw, result.energy);
        }
    } else {
        // Multi-tile: retune to each tile centre
        const double step  = usableBw;
        const double first = channels.front() + usableBw / 2.0;
        const double last  = channels.back() - usableBw / 2.0;
        for (double tc = first; tc <= last + step * 0.5; tc += step) {
            retune_source(soapy_source, tc, cfg.settle_ms);
            update_spectrum_centre(spectrum, tc);
            for (int snap = 0; snap < kL1Snapshots; ++snap) {
                extractTileEnergy(spectrum, channels, chBw, result.energy);
            }
        }
        // Return to default tile centre
        const double tileCentre = (channels.front() + channels.back()) / 2.0;
        retune_source(soapy_source, tileCentre, cfg.settle_ms);
        update_spectrum_centre(spectrum, tileCentre);
    }

    // Hot channel detection (same threshold logic as before)
    constexpr double    kL1Multiplier = 6.0;
    std::vector<double> sorted(result.energy.begin(), result.energy.end());
    std::ranges::sort(sorted);
    const double median = (nCh % 2 == 1) ? sorted[nCh / 2] : (sorted[nCh / 2 - 1] + sorted[nCh / 2]) / 2.0;
    const double thresh = median * kL1Multiplier;

    std::vector<bool> isHot(nCh, false);
    for (std::size_t idx = 0; idx < nCh; ++idx) {
        if (result.energy[idx] > thresh) {
            isHot[idx] = true;
        }
    }

    // Wideband aggregation
    for (const double bw : cfg.bws) {
        const auto groupSize = static_cast<std::size_t>(std::round(bw / chBw));
        if (groupSize <= 1) {
            continue;
        }
        for (std::size_t start = 0; start + groupSize <= nCh; ++start) {
            double groupSum = 0.0;
            for (std::size_t k = 0; k < groupSize; ++k) {
                groupSum += result.energy[start + k];
            }
            const double groupAvg = groupSum / static_cast<double>(groupSize);
            if (groupAvg > thresh) {
                const std::size_t center = start + groupSize / 2;
                if (!isHot[center]) {
                    isHot[center] = true;
                }
            }
        }
    }

    for (std::size_t idx = 0; idx < nCh; ++idx) {
        if (isHot[idx]) {
            result.hot_idx.push_back(static_cast<int>(idx));
        }
    }
    std::ranges::sort(result.hot_idx, [&](int a, int b) { return result.energy[static_cast<std::size_t>(a)] > result.energy[static_cast<std::size_t>(b)]; });
    for (int idx : result.hot_idx) {
        gr::lora::log_ts("debug", "lora_scan", "L1 HOT %.0f Hz  energy=%.6f (thresh=%.6f)", channels[static_cast<std::size_t>(idx)], result.energy[static_cast<std::size_t>(idx)], thresh);
    }
    return result;
}

// ─── Layer 2: per-channel CAD dwell — multi-SF on each window ────────────────

/// Build a ChannelActivityDetector pre-initialized for multi-SF detection.
static gr::lora::ChannelActivityDetector buildMultiSfDetector(double bw, uint32_t osFactor) {
    gr::lora::ChannelActivityDetector cad;
    cad.sf         = 12U; // start() needs a valid SF; detectMultiSf uses its own table
    cad.bandwidth  = static_cast<uint32_t>(bw);
    cad.os_factor  = osFactor;
    cad.dual_chirp = true;
    cad.debug      = false;
    cad.start();
    cad.initMultiSf();
    return cad;
}

struct DirectCadResult {
    double   freq          = 0.0;
    double   bw            = 0.0;
    double   peak_ratio_up = 0.0;
    double   peak_ratio_dn = 0.0;
    uint32_t sf_detected   = 0;
    bool     up_detected   = false;
    bool     dn_detected   = false;
    // Preamble characterization (valid when preamble_valid == true)
    bool     preamble_valid = false;
    uint16_t sync_word      = 0;
    float    cfo_frac       = 0.f;
    int      cfo_int        = 0;
    float    sto_frac       = 0.f;
    float    sfo_hat        = 0.f;
    uint32_t preamble_len   = 0;
    float    snr_db         = -999.f;
};

// ─── Mode B: interleaved L1 snapshot + immediate L2 ─────────────────────────

struct InterleavedResult {
    std::vector<DirectCadResult> detections;
    std::vector<double>          energy;
    uint32_t                     l1_hot_total = 0;
    uint32_t                     l2_probes    = 0;
};

struct L2BwResult {
    double      bw;
    uint32_t    sf;
    double      ratio;
    const char* chirp;
};

struct SweepCallbacks {
    std::function<void(double freq, const char* reason)>                           onRetune;
    std::function<void(double freq, uint32_t durationMs, std::vector<L2BwResult>)> onL2Probe;
    std::function<void(int snap, int total, uint32_t hotCount)>                    onProgress;
};

static InterleavedResult interleavedSweep(ScanGraph& sg, std::shared_ptr<gr::BlockModel>& soapy_source, const std::vector<double>& channels, const ScanSetConfig& cfg, const std::atomic<bool>& stopFlag, const SweepCallbacks& callbacks = {}) {
    InterleavedResult ir;

    const std::size_t nCh  = channels.size();
    const double      chBw = cfg.min_bw();
    ir.energy.resize(nCh, 0.0);

    struct BwDetector {
        double                            bw;
        gr::lora::ChannelActivityDetector cad;
    };
    std::vector<BwDetector> bwDets;
    for (const double bw : cfg.bws) {
        bwDets.push_back({bw, buildMultiSfDetector(bw, cfg.os_factor)});
    }

    const double tileCentre = (channels.front() + channels.back()) / 2.0;

    constexpr double kL1Multiplier = 6.0;
    const int        kSnapshots    = static_cast<int>(2U * nCh * cfg.bws.size());

    std::vector<double> energy(nCh, 0.0);
    std::vector<bool>   probed(nCh, false);

    // Ensure we're tuned to tile centre
    retune_source(soapy_source, tileCentre, cfg.settle_ms);
    update_spectrum_centre(sg.spectrum, tileCentre);

    for (int snap = 0; snap < kSnapshots; ++snap) {
        if (stopFlag.load(std::memory_order_relaxed)) {
            break;
        }

        // Single FFT snapshot: extract fresh per-channel energy, then
        // update running-max for CBOR spectrum output.
        std::ranges::fill(energy, 0.0);
        if (!extractTileEnergy(sg.spectrum, channels, chBw, energy, 50)) {
            continue; // spectrum not ready (overflow), skip this snapshot
        }
        for (std::size_t i = 0; i < nCh; ++i) {
            ir.energy[i] = std::max(ir.energy[i], energy[i]);
        }

        std::vector<double> sorted(energy.begin(), energy.end());
        std::ranges::sort(sorted);
        const double median = (nCh % 2 == 1) ? sorted[nCh / 2] : (sorted[nCh / 2 - 1] + sorted[nCh / 2]) / 2.0;
        const double thresh = median * kL1Multiplier;

        int    bestIdx    = -1;
        double bestEnergy = 0.0;

        for (std::size_t idx = 0; idx < nCh; ++idx) {
            if (!probed[idx] && energy[idx] > thresh && energy[idx] > bestEnergy) {
                bestIdx    = static_cast<int>(idx);
                bestEnergy = energy[idx];
            }
        }

        for (const double bw : cfg.bws) {
            const auto groupSize = static_cast<std::size_t>(std::round(bw / chBw));
            if (groupSize <= 1) {
                continue;
            }
            for (std::size_t start = 0; start + groupSize <= nCh; ++start) {
                const std::size_t center = start + groupSize / 2;
                if (probed[center]) {
                    continue;
                }
                double groupSum = 0.0;
                for (std::size_t k = 0; k < groupSize; ++k) {
                    groupSum += energy[start + k];
                }
                const double groupAvg = groupSum / static_cast<double>(groupSize);
                if (groupAvg > thresh && groupAvg > bestEnergy) {
                    bestIdx    = static_cast<int>(center);
                    bestEnergy = groupAvg;
                }
            }
        }

        if (bestIdx < 0) {
            if (callbacks.onProgress && (snap % 100 == 0)) {
                callbacks.onProgress(snap + 1, kSnapshots, ir.l1_hot_total);
            }
            continue;
        }

        ++ir.l1_hot_total;
        const double freq = channels[static_cast<std::size_t>(bestIdx)];

        gr::lora::log_ts("debug", "lora_scan", "B: snap %d/%d  HOT %.0f Hz  energy=%.6f (thresh=%.6f)  -> immediate L2", snap + 1, kSnapshots, freq, bestEnergy, thresh);

        double                  bestRatio = 0.0;
        DirectCadResult         bestResult;
        std::vector<L2BwResult> bwResults;

        // Retune to hot channel for L2 CAD
        retune_source(soapy_source, freq, cfg.settle_ms);
        if (callbacks.onRetune) {
            callbacks.onRetune(freq, "l2_probe");
        }

        const auto probeStart = std::chrono::steady_clock::now();

        // Phase 1: Quick CAD — 2-symbol capture (~131ms at BW62.5k)
        constexpr uint32_t kCadSymbols   = 2U;
        const uint32_t     sf12CadWin    = (1U << 12U) * cfg.os_factor * kCadSymbols;
        const double       maxTargetRate = bwDets.front().bw * static_cast<double>(cfg.os_factor);
        const auto         maxDecFactor  = static_cast<uint32_t>(std::round(cfg.l1_rate / maxTargetRate));
        const uint32_t     maxL1Win      = sf12CadWin * maxDecFactor;

        const auto l2Raw = capture_samples(sg.capture, maxL1Win, stopFlag);
        if (l2Raw.empty()) {
            retune_source(soapy_source, tileCentre, cfg.settle_ms);
            if (callbacks.onRetune) {
                callbacks.onRetune(tileCentre, "l1_restore");
            }
            continue;
        }

        double bestBw = 0.0;

        for (auto& bd : bwDets) {
            if (stopFlag.load(std::memory_order_relaxed)) {
                break;
            }

            const double   targetRate = bd.bw * static_cast<double>(cfg.os_factor);
            const auto     decFactor  = static_cast<uint32_t>(std::round(cfg.l1_rate / targetRate));
            const uint32_t l1Win      = sf12CadWin * decFactor;

            // Decimate a prefix of the single capture
            const auto cadBuf = decimate(l2Raw.data(), l1Win, cfg.l1_rate, targetRate);

            const auto r = bd.cad.detectMultiSf(cadBuf.data());
            if (r.detected) {
                const double ratio = static_cast<double>(r.best_ratio);
                if (ratio > bestRatio) {
                    bestRatio                = ratio;
                    bestResult.freq          = freq;
                    bestResult.bw            = bd.bw;
                    bestResult.peak_ratio_up = static_cast<double>(r.peak_ratio_up);
                    bestResult.peak_ratio_dn = static_cast<double>(r.peak_ratio_dn);
                    bestResult.sf_detected   = r.sf;
                    bestResult.up_detected   = r.up_detected;
                    bestResult.dn_detected   = r.dn_detected;
                    bestBw                   = bd.bw;
                }
            }

            {
                const double ratio = r.detected ? static_cast<double>(r.best_ratio) : 0.0;
                const char*  chirp = !r.detected ? "" : (r.up_detected && r.dn_detected) ? "both" : r.dn_detected ? "dn" : "up";
                bwResults.push_back({bd.bw, r.sf, ratio, chirp});
            }

            if (bestRatio > 10.0) {
                break;
            }
        }

        // Phase 2: Extended capture for preamble characterization (only on detection)
        if (bestResult.sf_detected > 0 && bestRatio >= static_cast<double>(cfg.min_ratio)) {
            // Second capture with 11-symbol window for full preamble
            constexpr uint32_t kPreambleSymbols = 11U;
            const uint32_t     sf12PreWin       = (1U << 12U) * cfg.os_factor * kPreambleSymbols;
            const double       preTargetRate    = bestBw * static_cast<double>(cfg.os_factor);
            const auto         preDecFactor     = static_cast<uint32_t>(std::round(cfg.l1_rate / preTargetRate));
            const uint32_t     preL1Win         = sf12PreWin * preDecFactor;

            const auto preRaw = capture_samples(sg.capture, preL1Win, stopFlag);
            if (!preRaw.empty()) {
                const auto preBuf = decimate(preRaw.data(), preL1Win, cfg.l1_rate, preTargetRate);
                auto       pinfo  = gr::lora::characterize_preamble(preBuf.data(), preBuf.size(), static_cast<uint8_t>(bestResult.sf_detected), static_cast<uint32_t>(bestBw), static_cast<uint8_t>(cfg.os_factor), freq);
                if (pinfo.valid) {
                    bestResult.preamble_valid = true;
                    bestResult.sync_word      = pinfo.sync_word;
                    bestResult.cfo_frac       = pinfo.cfo_frac;
                    bestResult.cfo_int        = pinfo.cfo_int;
                    bestResult.sto_frac       = pinfo.sto_frac;
                    bestResult.sfo_hat        = pinfo.sfo_hat;
                    bestResult.preamble_len   = pinfo.preamble_len;
                    bestResult.snr_db         = pinfo.snr_db;
                }
            }
        }

        const auto probeEnd = std::chrono::steady_clock::now();
        const auto probeMs  = static_cast<uint32_t>(std::chrono::duration_cast<std::chrono::milliseconds>(probeEnd - probeStart).count());

        if (callbacks.onL2Probe) {
            callbacks.onL2Probe(freq, probeMs, std::move(bwResults));
        }

        // Return to tile centre for next L1 snapshot
        retune_source(soapy_source, tileCentre, cfg.settle_ms);
        update_spectrum_centre(sg.spectrum, tileCentre);
        if (callbacks.onRetune) {
            callbacks.onRetune(tileCentre, "l1_restore");
        }

        probed[static_cast<std::size_t>(bestIdx)] = true;
        ++ir.l2_probes;

        if (bestRatio > 0.0) {
            ir.detections.push_back(bestResult);
        }
    }

    return ir;
}

// ─── CBOR output ──────────────────────────────────────────────────────────────

// Shared output state — set in main(), used by all emitters.
static UdpState* g_udp         = nullptr;
static bool      g_cbor_stdout = false;

static void sendCbor(const std::vector<uint8_t>& buf) {
    if (g_udp) {
        g_udp->broadcast_all(buf);
    }
    if (g_cbor_stdout) {
        std::fwrite(buf.data(), 1, buf.size(), stdout);
        std::fflush(stdout);
    }
}

static void emitSpectrumCbor(const std::vector<double>& channels, const std::vector<double>& energy, const std::vector<std::size_t>& hotIndices, const std::vector<DirectCadResult>& detections, uint32_t sweepCount) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_spectrum", 7);
    cb::kv_uint(buf, "sweep", sweepCount);
    cb::kv_uint(buf, "freq_min", static_cast<uint64_t>(channels.front()));

    const uint64_t step = channels.size() > 1 ? static_cast<uint64_t>(channels[1] - channels[0]) : 62500;
    cb::kv_uint(buf, "freq_step", step);

    const auto           nCh = static_cast<uint32_t>(energy.size());
    std::vector<uint8_t> floatBuf(nCh * sizeof(float));
    for (uint32_t i = 0; i < nCh; ++i) {
        auto val = static_cast<float>(energy[i]);
        std::memcpy(floatBuf.data() + i * sizeof(float), &val, sizeof(float));
    }
    cb::kv_bytes(buf, "channels", floatBuf.data(), floatBuf.size());
    cb::kv_uint(buf, "n_channels", nCh);

    cb::encode_text(buf, "hot");
    cb::encode_array_begin(buf, hotIndices.size());
    for (auto idx : hotIndices) {
        cb::encode_uint(buf, static_cast<uint64_t>(idx));
    }

    cb::encode_text(buf, "detections");
    cb::encode_array_begin(buf, detections.size());
    for (const auto& det : detections) {
        const double   ratio   = std::max(det.peak_ratio_up, det.peak_ratio_dn);
        const double   k       = det.bw * det.bw / static_cast<double>(1U << det.sf_detected);
        const uint32_t nFields = det.preamble_valid ? 12U : 5U;
        cb::encode_map_begin(buf, nFields);
        cb::kv_uint(buf, "freq", static_cast<uint64_t>(det.freq));
        cb::kv_float64(buf, "ratio", ratio);
        cb::kv_float64(buf, "chirp_slope", k);
        cb::kv_uint(buf, "probe_bw", static_cast<uint64_t>(det.bw));
        cb::kv_uint(buf, "sf", det.sf_detected);
        if (det.preamble_valid) {
            cb::kv_uint(buf, "sync_word", det.sync_word);
            cb::kv_float64(buf, "cfo_frac", static_cast<double>(det.cfo_frac));
            cb::kv_sint(buf, "cfo_int", det.cfo_int);
            cb::kv_float64(buf, "sto_frac", static_cast<double>(det.sto_frac));
            cb::kv_float64(buf, "sfo_hat", static_cast<double>(det.sfo_hat));
            cb::kv_uint(buf, "preamble_len", det.preamble_len);
            cb::kv_float64(buf, "snr_db", static_cast<double>(det.snr_db));
        }
    }

    sendCbor(buf);
}

static void emitStatusCbor(const ScanSetConfig& cfg, const ScanStats& stats, const std::string& mode) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_status", 6);
    cb::kv_text(buf, "mode", mode);
    cb::kv_uint(buf, "sweeps", stats.sweeps);
    cb::kv_uint(buf, "detections", stats.total_detections);
    cb::kv_uint(buf, "overflows", stats.overflows);
    cb::kv_uint(buf, "dropouts", stats.drops);

    cb::encode_text(buf, "scan_range");
    cb::encode_array_begin(buf, 2);
    cb::encode_uint(buf, static_cast<uint64_t>(cfg.freq_start));
    cb::encode_uint(buf, static_cast<uint64_t>(cfg.freq_stop));

    sendCbor(buf);
}

// ─── CBOR event emitters (real-time diagnostics) ─────────────────────────────

static void emitSweepStartCbor(uint32_t sweep) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_sweep_start", 1);
    cb::kv_uint(buf, "sweep", sweep);
    sendCbor(buf);
}

static void emitSweepEndCbor(uint32_t sweep, uint32_t durationMs, uint32_t l1Snapshots, uint32_t l2Probes, uint32_t hotCount, uint32_t detections, uint32_t overflows) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_sweep_end", 7);
    cb::kv_uint(buf, "sweep", sweep);
    cb::kv_uint(buf, "duration_ms", durationMs);
    cb::kv_uint(buf, "l1_snapshots", l1Snapshots);
    cb::kv_uint(buf, "l2_probes", l2Probes);
    cb::kv_uint(buf, "hot_count", hotCount);
    cb::kv_uint(buf, "detections", detections);
    cb::kv_uint(buf, "overflows", overflows);
    sendCbor(buf);
}

static void emitL2ProbeCbor(uint32_t sweep, double freq, uint32_t durationMs, const std::vector<L2BwResult>& results) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_l2_probe", 4);
    cb::kv_uint(buf, "sweep", sweep);
    cb::kv_uint(buf, "freq", static_cast<uint64_t>(freq));
    cb::kv_uint(buf, "duration_ms", durationMs);
    cb::encode_text(buf, "results");
    cb::encode_array_begin(buf, results.size());
    for (const auto& r : results) {
        const double k = r.bw * r.bw / static_cast<double>(1U << r.sf);
        cb::encode_map_begin(buf, 3);
        cb::kv_float64(buf, "ratio", r.ratio);
        cb::kv_float64(buf, "chirp_slope", k);
        cb::kv_uint(buf, "probe_bw", static_cast<uint64_t>(r.bw));
    }
    sendCbor(buf);
}

static void emitRetuneCbor(uint32_t sweep, double freq, const char* reason) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_retune", 3);
    cb::kv_uint(buf, "sweep", sweep);
    cb::kv_uint(buf, "freq", static_cast<uint64_t>(freq));
    cb::kv_text(buf, "reason", reason);
    sendCbor(buf);
}

static void emitOverflowCbor(uint32_t sweep, uint64_t count) {
    namespace cb = gr::lora::cbor;
    auto buf     = gr::lora::telemetry::beginEvent("scan_overflow", 2);
    cb::kv_uint(buf, "sweep", sweep);
    cb::kv_uint(buf, "count", count);
    sendCbor(buf);
}

// ─── UTC time helper: "HH:MM:SS.mmm" ─────────────────────────────────────────

static std::string ts_short() {
    auto    now = std::chrono::system_clock::now();
    auto    t   = std::chrono::system_clock::to_time_t(now);
    auto    ms  = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    std::tm tm{};
    gmtime_r(&t, &tm);
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%02d:%02d:%02d.%03d", tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms.count()));
    return buf;
}

// ─── streaming scan mode ──────────────────────────────────────────────────────

// Lock-free handoff: scheduler thread stores DataSet + overflow count,
// main thread polls and emits CBOR (keeping blocking I/O off the scheduler).
struct CborHandoff {
    std::mutex         mtx;
    gr::DataSet<float> ds;
    uint64_t           overflows{0};
    bool               ready{false};
};

// Convert a DataSet from the streaming pipeline to CBOR and send via UDP/stdout.
static void emitDataSetCbor(const gr::DataSet<float>& ds, uint64_t overflows = 0) {
    if (ds.meta_information.empty()) {
        return;
    }
    const auto& meta = ds.meta_information[0];

    auto typeIt = meta.find("type");
    if (typeIt == meta.end()) {
        return;
    }
    const auto typeStr = typeIt->second.value_or(std::string{""});
    if (typeStr != "scan_spectrum") {
        return;
    }

    const auto nCh = static_cast<uint32_t>(ds.signal_values.size());
    if (nCh == 0) {
        return;
    }

    auto getInt = [&](const std::string& key) -> int64_t {
        auto it = meta.find(key);
        return (it != meta.end()) ? it->second.value_or<int64_t>(0) : 0;
    };
    auto getMetaFloat = [&](const std::string& key) -> float {
        auto it = meta.find(key);
        return (it != meta.end()) ? it->second.value_or<float>(0.f) : 0.f;
    };
    auto getMetaDouble = [&](const std::string& key) -> double {
        auto it = meta.find(key);
        return (it != meta.end()) ? it->second.value_or<double>(0.0) : 0.0;
    };

    namespace cb = gr::lora::cbor;

    // --- Parse hot channel indices from comma-separated string ---
    std::vector<uint64_t> hotIndices;
    {
        auto hotIt = meta.find("hot_indices");
        if (hotIt != meta.end()) {
            const auto  hotStr = hotIt->second.value_or(std::string{""});
            std::size_t pos    = 0;
            while (pos < hotStr.size()) {
                auto end = hotStr.find(',', pos);
                if (end == std::string::npos) {
                    end = hotStr.size();
                }
                if (end > pos) {
                    hotIndices.push_back(static_cast<uint64_t>(std::stoul(hotStr.substr(pos, end - pos))));
                }
                pos = end + 1;
            }
        }
    }

    // --- Parse detections from indexed metadata fields ---
    const auto nDet = static_cast<std::size_t>(getInt("n_det"));
    struct Det {
        uint64_t freq;
        double   ratio;
        double   chirp_slope;
        uint64_t probe_bw;
        uint64_t sf;
    };
    std::vector<Det> detections;
    for (std::size_t i = 0; i < nDet; ++i) {
        const auto prefix = std::string("det") + std::to_string(i) + "_";
        Det        d;
        d.freq        = static_cast<uint64_t>(getMetaDouble(prefix + "freq"));
        d.ratio       = static_cast<double>(getMetaFloat(prefix + "ratio"));
        d.chirp_slope = static_cast<double>(getMetaFloat(prefix + "chirp_slope"));
        d.probe_bw    = static_cast<uint64_t>(getMetaFloat(prefix + "probe_bw"));
        d.sf          = static_cast<uint64_t>(getInt(prefix + "sf"));
        detections.push_back(d);
    }

    // --- Emit scan_spectrum CBOR ---
    {
        float freqMin  = (!ds.axis_values.empty() && !ds.axis_values[0].empty()) ? ds.axis_values[0][0] : 0.f;
        float freqStep = (!ds.axis_values.empty() && ds.axis_values[0].size() > 1) ? ds.axis_values[0][1] - ds.axis_values[0][0] : 62500.f;

        auto buf = gr::lora::telemetry::beginEvent("scan_spectrum", 7);
        cb::kv_uint(buf, "sweep", static_cast<uint64_t>(getInt("sweep")));
        cb::kv_uint(buf, "freq_min", static_cast<uint64_t>(freqMin));
        cb::kv_uint(buf, "freq_step", static_cast<uint64_t>(freqStep));

        std::vector<uint8_t> floatBuf(nCh * sizeof(float));
        std::memcpy(floatBuf.data(), ds.signal_values.data(), nCh * sizeof(float));
        cb::kv_bytes(buf, "channels", floatBuf.data(), floatBuf.size());
        cb::kv_uint(buf, "n_channels", nCh);

        cb::encode_text(buf, "hot");
        cb::encode_array_begin(buf, hotIndices.size());
        for (auto idx : hotIndices) {
            cb::encode_uint(buf, idx);
        }

        cb::encode_text(buf, "detections");
        cb::encode_array_begin(buf, detections.size());
        for (const auto& d : detections) {
            cb::encode_map_begin(buf, 5);
            cb::kv_uint(buf, "freq", d.freq);
            cb::kv_float64(buf, "ratio", d.ratio);
            cb::kv_float64(buf, "chirp_slope", d.chirp_slope);
            cb::kv_uint(buf, "probe_bw", d.probe_bw);
            cb::kv_uint(buf, "sf", d.sf);
        }

        sendCbor(buf);
    }

    // --- Emit scan_sweep_end CBOR (updates header in lora_spectrum.py + lora_perf.py) ---
    {
        auto buf = gr::lora::telemetry::beginEvent("scan_sweep_end", 6);
        cb::kv_uint(buf, "sweep", static_cast<uint64_t>(getInt("sweep")));
        cb::kv_uint(buf, "duration_ms", static_cast<uint64_t>(getInt("duration_ms")));
        cb::kv_uint(buf, "detections", static_cast<uint64_t>(nDet));
        cb::kv_uint(buf, "hot_count", static_cast<uint64_t>(hotIndices.size()));
        cb::kv_uint(buf, "l1_snapshots", static_cast<uint64_t>(getInt("n_snapshots")));
        cb::kv_uint(buf, "overflows", overflows);

        sendCbor(buf);
    }
}

static int streaming_main(ScanSetConfig& cfg) {
    auto& g_stop = lora_apps::install_signal_handler();

    // UDP socket for CBOR broadcast
    UdpState udp;
    udp.fd = lora_apps::create_udp_socket(cfg.udp_listen, cfg.udp_port);
    if (udp.fd < 0) {
        return 1;
    }
    g_udp         = &udp;
    g_cbor_stdout = cfg.cbor_out;

    lora_apps::log_version_banner("lora_scan", GIT_REV);
    gr::lora::log_ts("info ", "lora_scan", "mode       streaming  @ %.1f MS/s, center %.3f MHz  (buffer %u ms)", cfg.l1_rate / 1e6, cfg.center_freq() / 1e6, static_cast<unsigned>(cfg.buffer_ms));
    gr::lora::log_ts("info ", "lora_scan", "telemetry  udp://%s:%u", cfg.udp_listen.c_str(), cfg.udp_port);

    int savedStdout = -1;
    if (cfg.cbor_out) {
        std::fflush(stdout);
        savedStdout = dup(STDOUT_FILENO);
        dup2(STDERR_FILENO, STDOUT_FILENO);
    }

    auto hw_info = lora_apps::log_hardware_info("lora_scan", cfg.device, cfg.device_param);

    gr::thread_pool::Manager::defaultCpuPool()->setThreadBounds(1, 1);

    // Build streaming scan graph
    gr::Graph graph;
    lora_graph::build_streaming_scan_graph(graph, cfg);

    gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking> sched;
    sched.timeout_inactivity_count = 10U; // SoapySource IO thread drives progress; log after 10s stall

    if (auto ret = sched.exchange(std::move(graph)); !ret) {
        gr::lora::log_ts("error", "lora_scan", "scheduler init failed");
        return 1;
    }

    // Find blocks post-exchange (original graph references are dangling)
    auto  soapy_source = lora_graph::find_soapy_source(sched.blocks());
    auto* scanSink     = lora_graph::find_scan_sink(sched.blocks());
    if (!scanSink) {
        gr::lora::log_ts("error", "lora_scan", "ScanSink not found in graph");
        return 1;
    }

    // CBOR handoff: scheduler thread stores DataSet, main thread emits.
    // This keeps blocking I/O (UDP sendto, stdout fwrite+fflush) off the
    // scheduler thread, preventing overflow cascades from CBOR emission.
    CborHandoff handoff;

    scanSink->_onDataSet = [&handoff, &soapy_source](const gr::DataSet<float>& ds) {
        uint64_t ovf = 0;
        if (soapy_source) {
            using SoapyType = gr::blocks::sdr::SoapySource<cf32, 1UZ>;
            auto* wrapper   = dynamic_cast<gr::BlockWrapper<SoapyType>*>(soapy_source.get());
            if (wrapper) {
                ovf = wrapper->blockRef()._overflowCount.load(std::memory_order_relaxed);
            }
        }
        {
            std::lock_guard<std::mutex> lock(handoff.mtx);
            handoff.ds        = ds;
            handoff.overflows = ovf;
            handoff.ready     = true;
        }
    };

    if (savedStdout >= 0) {
        dup2(savedStdout, STDOUT_FILENO);
        close(savedStdout);
        savedStdout = -1;
    }

    // Start scheduler in background thread
    std::atomic<bool> sched_done{false};
    std::thread       sched_thread([&sched, &sched_done]() {
        auto ret = sched.runAndWait();
        if (!ret.has_value()) {
            gr::lora::log_ts("warn ", "lora_scan", "scheduler stopped: %s", std::format("{}", ret.error()).c_str());
        }
        sched_done.store(true, std::memory_order_relaxed);
    });

    gr::lora::log_ts("info ", "lora_scan", "ready      streaming — Ctrl+C to stop");

    // Main thread: emit CBOR from handoff, accept UDP subscriptions, poll overflow
    uint64_t lastOvf = 0;
    while (!g_stop.load(std::memory_order_relaxed) && !sched_done.load(std::memory_order_relaxed)) {
        // Drain CBOR handoff — emit on main thread (blocking I/O is safe here)
        {
            gr::DataSet<float> pending_ds;
            uint64_t           pending_ovf = 0;
            bool               have        = false;
            {
                std::lock_guard<std::mutex> lock(handoff.mtx);
                if (handoff.ready) {
                    pending_ds    = std::move(handoff.ds);
                    pending_ovf   = handoff.overflows;
                    handoff.ready = false;
                    have          = true;
                }
            }
            if (have) {
                emitDataSetCbor(pending_ds, pending_ovf);
            }
        }

        // Accept UDP subscriptions (non-blocking)
        {
            struct sockaddr_storage sender{};
            socklen_t               sender_len = sizeof(sender);
            char                    tmp[256];
            auto                    n = ::recvfrom(udp.fd, tmp, sizeof(tmp), 0, reinterpret_cast<struct sockaddr*>(&sender), &sender_len);
            if (n >= 0) {
                udp.subscribe(sender);
            }
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(10));

        if (soapy_source) {
            using SoapyType = gr::blocks::sdr::SoapySource<cf32, 1UZ>;
            auto* wrapper   = dynamic_cast<gr::BlockWrapper<SoapyType>*>(soapy_source.get());
            if (wrapper) {
                uint64_t ovf = wrapper->blockRef()._overflowCount.load(std::memory_order_relaxed);
                if (ovf != lastOvf && (ovf == 1 || ovf % 50 == 0)) {
                    gr::lora::log_ts("warn ", "lora_scan", "overflow count: %llu", static_cast<unsigned long long>(ovf));
                }
                lastOvf = ovf;
            }
        }
    }

    gr::lora::log_ts("info ", "lora_scan", "stopping...");
    sched.requestStop();
    sched_thread.join();
    if (udp.fd >= 0) {
        ::close(udp.fd);
    }
    std::quick_exit(0);
}

// ─── main ─────────────────────────────────────────────────────────────────────

int main(int argc, char** argv) {
    // Install before any code that can throw so a UHD/libusb error
    // surfaces as a usable diagnostic instead of the libc++abi default
    // banner.  Mirrors lora_trx.cpp.
    lora_apps::install_terminate_handler("lora_scan");

    std::string config_path, log_level;
    bool        cbor_out = false;
    int         rc       = parse_scan_args(argc, argv, config_path, log_level, cbor_out);
    if (rc != 0) {
        return rc == 2 ? 0 : 1;
    }

    auto configs = lora_config::load_scan_config(config_path, log_level);
    if (configs.empty()) {
        return 1;
    }
    auto cfg     = std::move(configs[0]);
    cfg.cbor_out = cbor_out;

    // Streaming mode: no retune, digital channelization
    if (cfg.streaming) {
        return streaming_main(cfg);
    }

    // Tuning mode: imperative scan loop with hardware retune
    auto& g_stop = lora_apps::install_signal_handler();

    // --- UDP socket for CBOR event broadcast ---
    UdpState udp;
    udp.fd = lora_apps::create_udp_socket(cfg.udp_listen, cfg.udp_port);
    if (udp.fd < 0) {
        return 1;
    }
    g_udp         = &udp;
    g_cbor_stdout = cfg.cbor_out;

    int savedStdout = -1;

    try {
        const auto channels = makeChannelList(cfg.freq_start, cfg.freq_stop, cfg.min_bw());

        std::string bwListStr;
        for (std::size_t i = 0; i < cfg.bws.size(); ++i) {
            if (i > 0) {
                bwListStr += ",";
            }
            char tmp[32];
            std::snprintf(tmp, sizeof(tmp), "%u", cfg.bws[i]);
            bwListStr += tmp;
        }

        lora_apps::log_version_banner("lora_scan", GIT_REV);

        gr::lora::log_ts("info ", "lora_scan", "channels   %zu  (SF7-12, BW=[%s] Hz)", channels.size(), bwListStr.c_str());
        gr::lora::log_ts("info ", "lora_scan", "rate       L1 %.1f MS/s, os %u", cfg.l1_rate / 1.0e6, cfg.os_factor);

        if (cfg.sweeps == 0) {
            gr::lora::log_ts("info ", "lora_scan", "mode       continuous — Ctrl+C to stop");
        } else {
            gr::lora::log_ts("info ", "lora_scan", "mode       %u sweep%s", cfg.sweeps, cfg.sweeps == 1 ? "" : "s");
        }
        gr::lora::log_ts("info ", "lora_scan", "telemetry  udp://%s:%u", cfg.udp_listen.c_str(), cfg.udp_port);

        // stdout protection: UHD/SoapySDR may print to stdout during device init
        if (cfg.cbor_out) {
            std::fflush(stdout);
            savedStdout = dup(STDOUT_FILENO);
            dup2(STDERR_FILENO, STDOUT_FILENO);
        }

        auto hw_info = lora_apps::log_hardware_info("lora_scan", cfg.device, cfg.device_param);

        // Shrink thread pool — singleThreadedBlocking doesn't use it
        gr::thread_pool::Manager::defaultCpuPool()->setThreadBounds(1, 1);

        // Build GR4 graph
        gr::Graph graph;
        auto      sg = lora_graph::build_scan_graph(graph, cfg, channels);

        gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking> sched;
        sched.timeout_inactivity_count = 10U; // SoapySource IO thread drives progress; log after 10s stall

        if (auto ret = sched.exchange(std::move(graph)); !ret) {
            gr::lora::log_ts("error", "lora_scan", "scheduler init failed");
            return 1;
        }

        // Find SoapySource for runtime retune (after exchange, original refs are dangling)
        auto soapy_source = lora_graph::find_soapy_source(sched.blocks());
        if (!soapy_source) {
            gr::lora::log_ts("error", "lora_scan", "SoapySource not found in graph");
            return 1;
        }

        if (savedStdout >= 0) {
            dup2(savedStdout, STDOUT_FILENO);
            close(savedStdout);
            savedStdout = -1;
        }

        // Start scheduler in background thread
        std::atomic<bool> sched_done{false};
        std::thread       sched_thread([&sched, &sched_done]() {
            auto ret = sched.runAndWait();
            if (!ret.has_value()) {
                gr::lora::log_ts("warn ", "lora_scan", "scheduler stopped: %s", std::format("{}", ret.error()).c_str());
            }
            sched_done.store(true, std::memory_order_relaxed);
        });

        // Give the graph a moment to start streaming before the orchestrator begins
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        ScanStats stats;

        // ── Layer 1 only mode ────────────────────────────────────────────────────
        if (cfg.layer1_only) {
            auto* out = stderr;
            std::fprintf(out, "%-15s  %12s  %6s\n", "freq_hz", "energy_lin", "L1");
            std::fprintf(out, "%s\n", std::string(38, '-').c_str());

            while (!g_stop.load(std::memory_order_relaxed) && !sched_done.load(std::memory_order_relaxed)) {
                const auto               l1 = layer1Scan(sg.spectrum, soapy_source, channels, cfg);
                std::vector<std::size_t> hotIndices;
                for (std::size_t idx = 0; idx < channels.size(); ++idx) {
                    const bool hot = std::ranges::find(l1.hot_idx, static_cast<int>(idx)) != l1.hot_idx.end();
                    if (hot) {
                        hotIndices.push_back(idx);
                    }
                    std::fprintf(out, "%-15.0f  %12.6f  %6s\n", channels[idx], l1.energy[idx], hot ? "HOT" : "");
                }
                std::fflush(out);
                emitSpectrumCbor(channels, l1.energy, hotIndices, {}, stats.sweeps);
                if (++stats.sweeps >= cfg.sweeps && cfg.sweeps > 0) {
                    break;
                }
            }
            sched.requestStop();
            sched_thread.join();
            if (udp.fd >= 0) {
                ::close(udp.fd);
            }
            std::quick_exit(0);
        }

        // ── Full scan: adaptive mode selection ─────────────────────────────────

        {
            auto* out = stderr;
            std::fprintf(out, "%-13s  %10s  %6s  %6s  %6s  %6s  %4s\n", "time", "freq_mhz", "bw_khz", "up", "dn", "wins", "sf");
            std::fprintf(out, "%s\n", std::string(60, '-').c_str());
            std::fflush(out);
        }

        const double scanRange = channels.back() - channels.front() + cfg.min_bw();
        const double usableBw  = cfg.l1_rate * 0.8;

        if (scanRange > usableBw) {
            gr::lora::log_ts("error", "lora_scan",
                "scan range %.1f MHz exceeds usable BW %.1f MHz at L1 rate %.1f MS/s "
                "-- increase [scan] l1_rate or narrow freq_start/freq_stop in config.toml",
                scanRange / 1.0e6, usableBw / 1.0e6, cfg.l1_rate / 1.0e6);
            sched.requestStop();
            sched_thread.join();
            return 1;
        }

        gr::lora::log_ts("info ", "lora_scan", "scan mode: single-pass  (%zu ch, range=%.1f MHz, tile=%.1f MHz)", channels.size(), scanRange / 1.0e6, usableBw / 1.0e6);

        std::vector<DirectCadResult> sweepDetections;

        auto reportDetection = [&](const DirectCadResult& det) {
            const double ratio = std::max(det.peak_ratio_up, det.peak_ratio_dn);
            if (ratio < static_cast<double>(cfg.min_ratio)) {
                return;
            }

            ++stats.total_detections;
            sweepDetections.push_back(det);

            const auto        ts    = ts_short();
            const std::string sfStr = det.sf_detected ? ("SF" + std::to_string(det.sf_detected)) : "-";

            auto* out = stderr;
            if (det.preamble_valid) {
                std::fprintf(out,
                    "%-13s  %10.3f  %4s  BW%gk  sync=0x%02X  "
                    "CFO=%+.1f+%d  STO=%+.2f  SNR=%.1f  ratio=%.1f  pre=%u\n",
                    ts.c_str(), det.freq / 1.0e6, sfStr.c_str(), det.bw / 1.0e3, det.sync_word, static_cast<double>(det.cfo_frac), det.cfo_int, static_cast<double>(det.sto_frac), static_cast<double>(det.snr_db), std::max(det.peak_ratio_up, det.peak_ratio_dn), det.preamble_len);
            } else {
                std::fprintf(out, "%-13s  %10.3f  %4s  BW%gk  ratio=%.1f  (CAD only)\n", ts.c_str(), det.freq / 1.0e6, sfStr.c_str(), det.bw / 1.0e3, std::max(det.peak_ratio_up, det.peak_ratio_dn));
            }
            std::fflush(out);

            char freqBuf[32];
            std::snprintf(freqBuf, sizeof(freqBuf), "%.3f MHz", det.freq / 1.0e6);
            stats.last_det_freq = freqBuf;
            stats.last_det_time = ts;
        };

        std::vector<double> sweepEnergy(channels.size(), 0.0);
        auto                lastCborStatus = std::chrono::steady_clock::now();
        double              avgSweepMs     = 0.0;

        // Overflow polling: typed access to SoapySource
        uint64_t lastOverflowCount = 0;
        auto     checkOverflows    = [&](uint32_t sweep) {
            // Access overflow count via the typed block inside the wrapper.
            // SoapySource<cf32, 1>::_overflowCount is atomic and cross-thread safe.
            using SoapyType = gr::blocks::sdr::SoapySource<std::complex<float>, 1UZ>;
            auto* wrapper   = dynamic_cast<gr::BlockWrapper<SoapyType>*>(soapy_source.get());
            if (!wrapper) {
                return;
            }
            const uint64_t current = wrapper->blockRef()._overflowCount.load(std::memory_order_relaxed);
            if (current > lastOverflowCount) {
                stats.overflows = static_cast<uint32_t>(current);
                emitOverflowCbor(sweep, current);
                lastOverflowCount = current;
            }
        };

        while (!g_stop.load(std::memory_order_relaxed) && !sched_done.load(std::memory_order_relaxed)) {
            // Accept UDP subscriptions (non-blocking)
            {
                struct sockaddr_storage sender{};
                socklen_t               sender_len = sizeof(sender);
                char                    tmp[256];
                auto                    n = ::recvfrom(udp.fd, tmp, sizeof(tmp), 0, reinterpret_cast<struct sockaddr*>(&sender), &sender_len);
                if (n >= 0) {
                    udp.subscribe(sender);
                }
            }

            sweepDetections.clear();
            std::ranges::fill(sweepEnergy, 0.0);

            const uint32_t sweepNum   = stats.sweeps + 1;
            const auto     sweepStart = std::chrono::steady_clock::now();
            uint32_t       l2Probes   = 0;

            emitSweepStartCbor(sweepNum);

            SweepCallbacks callbacks;
            callbacks.onRetune  = [sweepNum](double freq, const char* reason) { emitRetuneCbor(sweepNum, freq, reason); };
            callbacks.onL2Probe = [&, sweepNum](double freq, uint32_t ms, std::vector<L2BwResult> results) {
                emitL2ProbeCbor(sweepNum, freq, ms, results);
                checkOverflows(sweepNum);
            };

            const auto bResult = interleavedSweep(sg, soapy_source, channels, cfg, g_stop, callbacks);
            for (const auto& det : bResult.detections) {
                reportDetection(det);
            }
            stats.l1_hot = bResult.l1_hot_total;
            sweepEnergy  = bResult.energy;
            l2Probes     = bResult.l2_probes;

            ++stats.sweeps;

            const auto sweepMs = static_cast<uint32_t>(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - sweepStart).count());
            avgSweepMs         = (stats.sweeps <= 1) ? static_cast<double>(sweepMs) : avgSweepMs * 0.8 + static_cast<double>(sweepMs) * 0.2;

            {
                std::vector<std::size_t> hotIndices;
                if (!sweepEnergy.empty()) {
                    std::vector<double> sorted(sweepEnergy.begin(), sweepEnergy.end());
                    std::ranges::sort(sorted);
                    const auto   nCh    = sweepEnergy.size();
                    const double median = (nCh % 2 == 1) ? sorted[nCh / 2] : (sorted[nCh / 2 - 1] + sorted[nCh / 2]) / 2.0;
                    const double thresh = median * 6.0;
                    for (std::size_t idx = 0; idx < nCh; ++idx) {
                        if (sweepEnergy[idx] > thresh) {
                            hotIndices.push_back(idx);
                        }
                    }
                }

                const auto kTotalSnaps = static_cast<uint32_t>(2U * channels.size() * cfg.bws.size());
                emitSweepEndCbor(sweepNum, sweepMs, kTotalSnaps, l2Probes, stats.l1_hot, static_cast<uint32_t>(sweepDetections.size()), stats.overflows);

                // Poll for new UDP subscribers before emitting CBOR
                // (subscribers may connect during a long tuning sweep)
                {
                    struct sockaddr_storage sender{};
                    socklen_t               sender_len = sizeof(sender);
                    char                    tmp[256];
                    while (::recvfrom(udp.fd, tmp, sizeof(tmp), 0, reinterpret_cast<struct sockaddr*>(&sender), &sender_len) >= 0) {
                        udp.subscribe(sender);
                        sender_len = sizeof(sender);
                    }
                }

                emitSpectrumCbor(channels, sweepEnergy, hotIndices, sweepDetections, stats.sweeps);

                const auto now2 = std::chrono::steady_clock::now();
                if (stats.sweeps == 1 || (now2 - lastCborStatus) >= std::chrono::seconds(5)) {
                    lastCborStatus = now2;
                    emitStatusCbor(cfg, stats, "single-pass");
                }
            }

            checkOverflows(stats.sweeps);

            if (cfg.sweeps > 0 && stats.sweeps >= cfg.sweeps) {
                break;
            }

            gr::lora::log_ts("info ", "lora_scan", "sweep %u  dur %ums  hot %u/%zu  det %zu  OVF %u", stats.sweeps, sweepMs, stats.l1_hot, channels.size(), sweepDetections.size(), stats.overflows);
        }

        gr::lora::log_ts("info ", "lora_scan", "stopped after %u sweep(s), %u overflow(s), %u drop(s)", stats.sweeps, stats.overflows, stats.drops);

        sched.requestStop();
        sched_thread.join();
        if (udp.fd >= 0) {
            ::close(udp.fd);
        }
        std::quick_exit(0);
    } catch (const std::exception& e) {
        if (savedStdout >= 0) {
            dup2(savedStdout, STDOUT_FILENO);
            close(savedStdout);
        }
        std::fprintf(stderr, "fatal: %s\n", e.what());
        if (udp.fd >= 0) {
            ::close(udp.fd);
        }
        return 1;
    }

    if (udp.fd >= 0) {
        ::close(udp.fd);
    }
}
