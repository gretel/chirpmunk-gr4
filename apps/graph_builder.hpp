// SPDX-License-Identifier: ISC
//
// Shared GR4 graph construction for lora_trx and lora_scan.
//
// - build_rx_graph():   SoapySource → [Splitter] → MultiSfDecoder → FrameSink
// - build_scan_graph(): SoapySource → Splitter → SpectrumTap + CaptureSink
// - find_rx_blocks():   locate typed blocks after scheduler exchange()

#ifndef GR4_LORA_GRAPH_BUILDER_HPP
#define GR4_LORA_GRAPH_BUILDER_HPP

#include "config.hpp"

#include <gnuradio-4.0/Graph.hpp>
#include <gnuradio-4.0/Tensor.hpp>

#include <gnuradio-4.0/lora/CaptureSink.hpp>
#include <gnuradio-4.0/lora/FrameSink.hpp>
#include <gnuradio-4.0/lora/MultiSfDecoder.hpp>
#include <gnuradio-4.0/lora/ScanController.hpp>
#include <gnuradio-4.0/lora/ScanSink.hpp>
#include <gnuradio-4.0/lora/SpectrumTap.hpp>
#include <gnuradio-4.0/lora/SpectrumTapBlock.hpp>
#include <gnuradio-4.0/lora/Splitter.hpp>
#include <gnuradio-4.0/lora/log.hpp>

// SoapySource.hpp MUST come before common.hpp (common.hpp uses sdr::soapy types)
#include <gnuradio-4.0/sdr/SoapySource.hpp>
#include <gnuradio-4.0/testing/NullSources.hpp>

#include "common.hpp"

#include <atomic>
#include <complex>
#include <functional>
#include <memory>
#include <span>
#include <string>
#include <vector>

namespace lora_graph {

using lora_config::DecodeConfig;
using lora_config::ScanSetConfig;
using lora_config::TrxConfig;

using lora_apps::cf32;

using AppMultiSfDecoder = gr::lora::MultiSfDecoder;

// ─── RX decode graph (lora_trx) ──────────────────────────────────────────────

/// Creates a MultiSfDecoder → FrameSink chain.
/// `decoder_bw` is the RX bandwidth for this chain (from [trx.receive].bandwidths);
/// it is independent of the TX preset `cfg.bw`.
/// The `callback` signature includes sync_word so downstream fanout can filter
/// per chain (multi-chain = different sync_words on the same RF tune).
/// Returns reference to the MultiSfDecoder (for connecting upstream).
inline AppMultiSfDecoder& add_multisf_chain(gr::Graph& graph, const TrxConfig& cfg, uint32_t decoder_bw, int32_t rx_channel, const DecodeConfig& dc, std::function<void(const std::vector<uint8_t>&, bool, uint16_t)> callback, std::shared_ptr<gr::lora::SpectrumState> spectrum = nullptr, std::function<void(const gr::property_map&)> telemetry_cb = {}) {
    auto os = static_cast<uint8_t>(cfg.rate / static_cast<float>(decoder_bw));

    auto& decoder     = graph.emplaceBlock<AppMultiSfDecoder>();
    decoder.bandwidth = decoder_bw;
    // sync_word semantics: optional in DecodeConfig. Unset = promiscuous
    // (decoder skips sync compare in stage 4 and surfaces the observed sync
    // word per-frame via a tag). Set = strict filter.
    decoder.promiscuous  = !dc.sync_word.has_value();
    decoder.sync_word    = dc.sync_word.value_or(uint16_t{0});
    decoder.os_factor    = os;
    decoder.preamble_len = cfg.preamble;
    decoder.center_freq  = static_cast<uint32_t>(cfg.freq);
    decoder.rx_channel   = rx_channel;
    // sf_set: empty = sweep SF7-12; single entry = pin single SF;
    // multi-entry = non-contiguous subset (e.g. {8, 11}).
    decoder.sf_set          = dc.sf_set;
    decoder.debug           = cfg.debug;
    decoder.soft_decode     = cfg.soft_decode;
    decoder.use_aa_filter   = cfg.use_aa_filter;
    decoder._spectrum_state = std::move(spectrum);
    if (telemetry_cb) {
        decoder._telemetry = telemetry_cb;
    }

    // Apply block-level overrides from config
    if (auto it = dc.block_overrides.find("min_snr_db"); it != dc.block_overrides.end()) {
        decoder.min_snr_db = it->second.template value_or<float>(-10.f);
    }
    if (auto it = dc.block_overrides.find("max_symbols"); it != dc.block_overrides.end()) {
        decoder.max_symbols = static_cast<uint32_t>(it->second.template value_or<int64_t>(600));
    }

    // FrameSink sync_word: set to the configured filter when strict, or 0
    // when promiscuous.  In promiscuous mode MultiSfDecoder emits a per-frame
    // `sync_word` tag that FrameSink prefers over this property, so the CBOR
    // output carries the actually-observed sync word regardless.
    auto& sink           = graph.emplaceBlock<gr::lora::FrameSink>({
        {"sync_word", dc.sync_word.value_or(uint16_t{0})},
        {"phy_sf", uint8_t{0}}, // MultiSf: SF varies per frame, reported in tag
        {"phy_bw", decoder_bw},
        {"label", dc.label},
    });
    sink._frame_callback = callback;

    auto ok = [](std::expected<void, gr::Error> r) { return r.has_value(); };
    if (!ok(graph.connect<"out", "in">(decoder, sink))) {
        gr::lora::log_ts("error", "graph", "connect MultiSfDecoder -> FrameSink failed (rx_ch %d)", rx_channel);
    }
    return decoder;
}

/// Generalised RX graph builder.
///
/// Topology (per radio channel, single BW):
///   Source → MultiSfDecoder (SF7-12) → FrameSink
///
/// Topology (per radio channel, multi-BW):
///   Source → Splitter(N) → MultiSfDecoder (BW1, SF7-12) → FrameSink
///                        → MultiSfDecoder (BW2, SF7-12) → FrameSink
///
/// Returns pointer to source block's cumulative overflow counter.
inline std::atomic<gr::Size_t>* build_rx_graph(gr::Graph& graph, const TrxConfig& cfg, std::function<void(const std::vector<uint8_t>&, bool, uint16_t)> callback, std::shared_ptr<gr::lora::SpectrumState> spectrum = nullptr, std::atomic<bool>* channel_busy = nullptr, std::function<void(const gr::property_map&)> telemetry_cb = {}) {
    auto ok = [](std::expected<void, gr::Error> r) { return r.has_value(); };
    using std::string_literals::operator""s;

    const auto& chains    = cfg.rx_chains;
    const auto  nRadio    = cfg.rx_channels.size();
    const auto& bws       = cfg.rx_bandwidths;
    const auto  nBW       = bws.size();
    const auto  nChains   = chains.size();
    const auto  nBranches = nBW * nChains;

    // rx_antenna from config; empty = driver default
    const auto& antennae = cfg.rx_antenna;

    // Wire (nBW × nChains) MultiSfDecoder(s) per radio channel.
    // Topology:
    //   nBranches == 1         : Source port -> MultiSfDecoder (direct)
    //   nBranches  > 1         : Source port -> Splitter(nBranches) -> N decoders
    //
    // Spectrum tap + channel_busy (LBT) flag attach to the first branch of the
    // first radio only.  Other branches still decode frames but don't duplicate
    // the shared state.
    auto wireDecodeChains = [&](auto connectPort) {
        for (std::size_t r = 0; r < nRadio; r++) {
            bool firstRadio = (r == 0);

            // Branch identity in the rx_channel telemetry tag:
            //   r * 100 + b * 10 + c
            // Compact enough to read at a glance (3 decimal digits for
            // dual-RX × 10 BW × 10 chain) while still uniquely identifying
            // every (radio, BW, chain) tuple downstream.
            auto branchRxCh = [&](std::size_t b, std::size_t c) -> int32_t { return static_cast<int32_t>(cfg.rx_channels[r]) * 100 + static_cast<int32_t>(b) * 10 + static_cast<int32_t>(c); };

            if (nBranches == 1) {
                auto  rx_ch   = branchRxCh(0, 0);
                auto  spec    = firstRadio ? spectrum : nullptr;
                auto& decoder = add_multisf_chain(graph, cfg, bws[0], rx_ch, chains[0], callback, spec, telemetry_cb);
                if (firstRadio && channel_busy != nullptr) {
                    decoder.set_channel_busy_flag(channel_busy);
                }
                if (!connectPort(r, decoder)) {
                    gr::lora::log_ts("error", "graph", "connect source -> MultiSfDecoder failed (radio %zu)", r);
                }
            } else {
                auto& splitter = graph.emplaceBlock<gr::lora::Splitter>({
                    {"n_outputs", static_cast<gr::Size_t>(nBranches)},
                });
                if (!connectPort(r, splitter)) {
                    gr::lora::log_ts("error", "graph", "connect source -> Splitter failed (radio %zu)", r);
                }

                for (std::size_t b = 0; b < nBW; b++) {
                    for (std::size_t c = 0; c < nChains; c++) {
                        auto branch_idx = b * nChains + c;
                        auto rx_ch      = branchRxCh(b, c);
                        auto spec       = (firstRadio && branch_idx == 0) ? spectrum : nullptr;

                        auto& decoder = add_multisf_chain(graph, cfg, bws[b], rx_ch, chains[c], callback, spec, telemetry_cb);

                        if (firstRadio && branch_idx == 0 && channel_busy != nullptr) {
                            decoder.set_channel_busy_flag(channel_busy);
                        }

                        auto port = "out#"s + std::to_string(branch_idx);
                        if (!ok(graph.connect(splitter, port, decoder, "in"s))) {
                            gr::lora::log_ts("error", "graph", "connect Splitter -> MultiSfDecoder BW%u chain '%s' failed (radio %zu)", bws[b], chains[c].label.c_str(), r);
                        }
                    }
                }
            }
        }
    };

    // Create source and wire: 1 radio = SoapySimpleSource, 2 radios = SoapyDualSource
    std::atomic<gr::Size_t>* overflow_ptr = nullptr;

    if (nRadio == 1) {
        auto props = lora_apps::soapy_reliability_defaults();
        props.merge(gr::property_map{
            {"device", cfg.device},
            {"device_parameter", cfg.device_param},
            {"sample_rate", cfg.rate},
            {"frequency", std::vector<double>{cfg.freq}},
            {"rx_gains", std::vector<double>{cfg.gain_rx}},
            {"num_channels", gr::Size_t{1}},
            {"rx_antennae", antennae},
            {"clock_source", cfg.clock},
            {"lo_offset", cfg.lo_offset},
            {"dc_offset_mode", cfg.dc_offset_auto},
            // Auto-enable DSP DC blocker when lo_offset=0 — the driver can't
            // shift the DC spur out-of-band (e.g. PlutoSDR/SoapyPlutoPAPR),
            // so the IIR high-pass in SoapySource removes it in software.
            // When lo_offset != 0, the hardware DDC (UHD FPGA) already handles
            // it and the blocker is unnecessary.
            {"dc_blocker_enabled", cfg.lo_offset == 0.0},
            {"dc_blocker_cutoff", (cfg.lo_offset == 0.0) ? 2000.f : 10.f},
            // PPM sample-rate estimator: tracks B210's VCTCXO drift via an
            // LP-filtered sample-clock estimate, re-publishes corrected
            // sample_rate/frequency tags when the drift exceeds the
            // threshold. Pluto's ad9361-phy doesn't expose a usable
            // wallclock-vs-sample-clock reference over IIO (rate estimate
            // becomes noise), and the resulting tag storm appears to
            // starve the narrowband decoder chain. Keep enabled for UHD
            // where it was originally verified; disable for everything
            // else until per-driver calibration exists.
            {"ppm_estimator_cutoff", (cfg.device == "uhd") ? 0.5f : 0.0f},
            {"ppm_tag_threshold", 1.0f}, // throttle sample_rate/frequency tag storm (default 0.1)
        });
        auto& source = graph.emplaceBlock<gr::blocks::sdr::SoapySimpleSource<std::complex<float>>>(std::move(props));
        overflow_ptr = &source._overflowCount;
        wireDecodeChains([&graph, &source](std::size_t /*r*/, auto& downstream) { return graph.connect<"out", "in">(source, downstream).has_value(); });
    } else {
        // Pin MCR for dual-channel RX. UHD B210's auto-MCR renegotiation
        // during dual-channel set_rx_rate destabilises the AD9361 and causes
        // STREAM_ERROR at activateStream. 24 MHz fits under the stock-UHD
        // 2T2R ceiling of 30.72 MHz (which the uhd-oc fork relaxes via the
        // AD9361 overclock patch) while still giving integer decimation for
        // both decode rates (24M/250k=96, 24M/4M=6).
        const double dualRxMcr = 24.0e6;
        auto         props     = lora_apps::soapy_reliability_defaults();
        props.merge(gr::property_map{
            {"device", cfg.device},
            {"device_parameter", cfg.device_param},
            {"sample_rate", cfg.rate},
            {"master_clock_rate", dualRxMcr},
            {"frequency", std::vector<double>{cfg.freq, cfg.freq}},
            {"rx_gains", std::vector<double>{cfg.gain_rx, cfg.gain_rx}},
            {"num_channels", gr::Size_t{2}},
            {"rx_antennae", antennae},
            {"clock_source", cfg.clock},
            {"lo_offset", cfg.lo_offset},
            {"dc_offset_mode", cfg.dc_offset_auto},
            {"dc_blocker_enabled", cfg.lo_offset == 0.0},
            {"dc_blocker_cutoff", (cfg.lo_offset == 0.0) ? 2000.f : 10.f},
            {"ppm_estimator_cutoff", 0.5f}, // track crystal drift (Hz LP cutoff)
            {"ppm_tag_threshold", 1.0f},    // throttle sample_rate/frequency tag storm (default 0.1)
        });
        auto& source = graph.emplaceBlock<gr::blocks::sdr::SoapyDualSource<std::complex<float>>>(std::move(props));
        overflow_ptr = &source._overflowCount;
        wireDecodeChains([&graph, &source](std::size_t r, auto& downstream) {
            auto portName = "out#"s + std::to_string(r);
            return graph.connect(source, portName, downstream, "in"s).has_value();
        });
    }

    {
        std::string bw_list;
        for (std::size_t i = 0; i < nBW; i++) {
            if (i > 0) {
                bw_list += ",";
            }
            if (bws[i] >= 1000) {
                bw_list += std::to_string(bws[i] / 1000) + "k";
            } else {
                bw_list += std::to_string(bws[i]);
            }
        }
        std::string chain_list;
        for (std::size_t i = 0; i < nChains; i++) {
            if (i > 0) {
                chain_list += ",";
            }
            char        buf[32];
            const auto& sw = chains[i].sync_word;
            std::string sf_label;
            if (chains[i].sf_set.empty()) {
                sf_label = "SF7-12";
            } else if (chains[i].sf_set.size() == 1) {
                sf_label = "SF" + std::to_string(chains[i].sf_set[0]);
            } else {
                sf_label = "SF{…}";
            }
            if (sw.has_value()) {
                std::snprintf(buf, sizeof(buf), "0x%02X/%s", static_cast<unsigned>(*sw), sf_label.c_str());
            } else {
                std::snprintf(buf, sizeof(buf), "any/%s", sf_label.c_str());
            }
            chain_list += chains[i].label + "(" + buf + ")";
        }
        gr::lora::log_ts("debug", "graph", "RX graph: %zu radio(s) x %zu BW(s) [%s] x %zu chain(s) [%s] = %zu decoder(s) per radio", nRadio, nBW, bw_list.c_str(), nChains, chain_list.c_str(), nBranches);
    }

    return overflow_ptr;
}

// ─── Post-exchange block discovery ───────────────────────────────────────────

/// Typed block references found after scheduler exchange().
struct RxBlocks {
    std::vector<std::shared_ptr<gr::BlockModel>> multisf_decoders;
    std::shared_ptr<gr::BlockModel>              soapy_source;
};

/// Scan the scheduler's block list and collect references by type name.
inline RxBlocks find_rx_blocks(std::span<std::shared_ptr<gr::BlockModel>> blocks) {
    RxBlocks rx;
    for (auto& blk : blocks) {
        auto tn = blk->typeName();
        if (tn.find("MultiSfDecoder") != std::string_view::npos) {
            rx.multisf_decoders.push_back(blk);
        } else if (tn.find("Soapy") != std::string_view::npos) {
            rx.soapy_source = blk;
        }
    }
    return rx;
}

/// Find the SoapySource block in a scheduler's block list.
inline std::shared_ptr<gr::BlockModel> find_soapy_source(std::span<std::shared_ptr<gr::BlockModel>> blocks) {
    for (auto& blk : blocks) {
        if (blk->typeName().find("Soapy") != std::string_view::npos) {
            return blk;
        }
    }
    return nullptr;
}

/// Find ScanSink in the scheduler's block list (for wiring callbacks post-exchange).
inline gr::lora::ScanSink* find_scan_sink(std::span<std::shared_ptr<gr::BlockModel>> blocks) {
    for (auto& blk : blocks) {
        if (blk->typeName().find("ScanSink") != std::string_view::npos) {
            auto* wrapper = dynamic_cast<gr::BlockWrapper<gr::lora::ScanSink>*>(blk.get());
            if (wrapper) {
                return &wrapper->blockRef();
            }
        }
    }
    return nullptr;
}

/// Set `_device_serial` on all FrameSink blocks in the scheduler.
inline void set_device_serial(std::span<std::shared_ptr<gr::BlockModel>> blocks, const std::string& serial) {
    if (serial.empty()) {
        return;
    }
    for (auto& blk : blocks) {
        if (blk->typeName().find("FrameSink") != std::string_view::npos) {
            auto* wrapper = dynamic_cast<gr::BlockWrapper<gr::lora::FrameSink>*>(blk.get());
            if (wrapper) {
                wrapper->blockRef()._device_serial = serial;
            }
        }
    }
}

// ─── Scan graph (lora_scan) ──────────────────────────────────────────────────

/// Shared state handles returned by build_scan_graph().
struct ScanGraph {
    std::shared_ptr<gr::lora::SpectrumState> spectrum;
    std::shared_ptr<gr::lora::CaptureState>  capture;
};

/// Build the scan graph: SoapySource → Splitter → SpectrumTap + CaptureSink
inline ScanGraph build_scan_graph(gr::Graph& graph, const ScanSetConfig& cfg, const std::vector<double>& channels) {
    auto ok = [](std::expected<void, gr::Error> r) { return r.has_value(); };
    using std::string_literals::operator""s;

    ScanGraph sg;

    // SoapySource -- fixed L1 rate, pinned master clock
    const double tileCentre   = (channels.front() + channels.back()) / 2.0;
    auto         source_props = lora_apps::soapy_reliability_defaults();
    source_props.merge(gr::property_map{
        {"device", cfg.device},
        {"device_parameter", cfg.device_param},
        {"sample_rate", cfg.l1_rate},
        {"master_clock_rate", cfg.master_clock},
        {"frequency", std::vector<double>{tileCentre}},
        {"rx_gains", std::vector<double>{cfg.gain}},
        {"verbose_overflow", false},
        {"overflow_recovery", std::string("log_only")},
        {"overflow_reactivate_threshold", gr::Size_t{5U}},
    });
    if (!cfg.clock.empty()) {
        source_props["clock_source"] = cfg.clock;
    }
    if (cfg.lo_offset != 0.0) {
        source_props["lo_offset"] = cfg.lo_offset;
    }
    source_props["dc_offset_mode"] = cfg.dc_offset_auto;
    if (cfg.lo_offset == 0.0) {
        source_props["dc_blocker_enabled"] = true;
        source_props["dc_blocker_cutoff"]  = 2000.f;
    }
    if (cfg.sweeps > 0) {
        // Finite-sweep mode: close the stream cleanly at end-of-sweep instead of
        // relying on quick_exit(0) ordering during teardown.
        source_props["disconnect_on_done"] = true;
    }
    auto& source = graph.emplaceBlock<gr::blocks::sdr::SoapySimpleSource<cf32>>(std::move(source_props));

    // Splitter -> 2 outputs
    auto& splitter = graph.emplaceBlock<gr::lora::Splitter>({
        {"n_outputs", gr::Size_t{2}},
    });
    if (!ok(graph.connect<"out", "in">(source, splitter))) {
        gr::lora::log_ts("error", "graph", "connect source -> splitter failed");
    }

    // Path 0: SpectrumTap -> NullSink (L1 energy)
    sg.spectrum              = std::make_shared<gr::lora::SpectrumState>();
    sg.spectrum->fft_size    = 4096;
    sg.spectrum->sample_rate = static_cast<float>(cfg.l1_rate);
    sg.spectrum->center_freq = tileCentre;
    sg.spectrum->init();

    auto& tap     = graph.emplaceBlock<gr::lora::SpectrumTapBlock>({});
    tap._spectrum = sg.spectrum;
    // Rate-limit ring push at high sample rates: target ~30 FFTs/s.
    // At 16 MS/s / 8192-sample chunks = ~1950 calls/s → interval = 64.
    tap._pushInterval = std::max(1U, static_cast<uint32_t>(cfg.l1_rate / (8192.0 * 30.0)));

    auto& null_sink = graph.emplaceBlock<gr::testing::NullSink<cf32>>({});

    if (!ok(graph.connect(splitter, "out#0"s, tap, "in"s))) {
        gr::lora::log_ts("error", "graph", "connect splitter -> tap failed");
    }
    if (!ok(graph.connect<"out", "in">(tap, null_sink))) {
        gr::lora::log_ts("error", "graph", "connect tap -> null_sink failed");
    }

    // Path 1: CaptureSink (L2 on-demand)
    // Buffer sized for full preamble characterization: 11 symbols at SF12
    // (8 upchirps + 2 sync + 0.25 quarter_down + margin) × os_factor × decFactor.
    // The actual capture request may be smaller (2-symbol CAD) or larger (11-symbol
    // preamble characterization) — the buffer just needs to be big enough for the max.
    sg.capture                          = std::make_shared<gr::lora::CaptureState>();
    const double       minBw            = cfg.min_bw();
    const double       targetRate       = minBw * static_cast<double>(cfg.os_factor);
    const auto         decFactor        = static_cast<uint32_t>(std::round(cfg.l1_rate / targetRate));
    constexpr uint32_t kPreambleSymbols = 11U; // full preamble + sync + margin
    const uint32_t     sf12PreambleWin  = (1U << 12U) * cfg.os_factor * kPreambleSymbols;
    sg.capture->buffer.resize(sf12PreambleWin * decFactor);

    auto& cap_sink  = graph.emplaceBlock<gr::lora::CaptureSink>({});
    cap_sink._state = sg.capture;

    if (!ok(graph.connect(splitter, "out#1"s, cap_sink, "in"s))) {
        gr::lora::log_ts("error", "graph", "connect splitter -> capture failed");
    }

    return sg;
}

/// Build the streaming scan graph: SoapySource → ScanController → ScanSink
/// No hardware retune — L2 uses digital channelization from the wideband stream.
/// ScanController handles both L1 energy (internal FFT) and L2 CAD.
/// Use find_scan_sink() post-exchange to wire the _onDataSet callback.
inline void build_streaming_scan_graph(gr::Graph& graph, const ScanSetConfig& cfg) {
    auto ok = [](std::expected<void, gr::Error> r) { return r.has_value(); };

    const double centerFreq = cfg.center_freq();

    // SoapySource at L1 wideband rate, fixed center frequency
    auto source_props = lora_apps::soapy_reliability_defaults();
    source_props.merge(gr::property_map{
        {"device", cfg.device},
        {"device_parameter", cfg.device_param},
        {"sample_rate", cfg.l1_rate},
        {"master_clock_rate", cfg.master_clock},
        {"frequency", std::vector<double>{centerFreq}},
        {"rx_gains", std::vector<double>{cfg.gain}},
        {"verbose_overflow", false},
        {"overflow_recovery", std::string("log_only")},
        {"overflow_reactivate_threshold", gr::Size_t{5U}},
    });
    if (!cfg.clock.empty()) {
        source_props["clock_source"] = cfg.clock;
    }
    if (cfg.lo_offset != 0.0) {
        source_props["lo_offset"] = cfg.lo_offset;
    }
    source_props["dc_offset_mode"] = cfg.dc_offset_auto;
    if (cfg.lo_offset == 0.0) {
        source_props["dc_blocker_enabled"] = true;
        source_props["dc_blocker_cutoff"]  = 2000.f;
    }
    auto& source = graph.emplaceBlock<gr::blocks::sdr::SoapySimpleSource<cf32>>(std::move(source_props));

    // ScanController: sole downstream block (IQ sink with ring buffer + L1/L2)
    // Build probe_bws string from config bws vector
    std::string probeBwsStr;
    for (std::size_t i = 0; i < cfg.bws.size(); ++i) {
        if (i > 0) {
            probeBwsStr += ',';
        }
        probeBwsStr += std::to_string(cfg.bws[i]);
    }
    if (probeBwsStr.empty()) {
        probeBwsStr = "62500";
    }

    auto& controller = graph.emplaceBlock<gr::lora::ScanController>({
        {"sample_rate", static_cast<float>(cfg.l1_rate)},
        {"center_freq", static_cast<float>(centerFreq)},
        {"min_ratio", cfg.min_ratio},
        {"buffer_ms", cfg.buffer_ms},
        {"channel_bw", cfg.channel_bw},
        {"l1_interval", uint32_t{16}}, // L1 FFT every 16 calls = ~8ms
        {"l1_snapshots", uint32_t{4}}, // 4 snapshots = ~33ms sweep
        {"l1_fft_size", cfg.l1_fft_size},
        {"probe_bws", probeBwsStr},
    });

    if (!ok(graph.connect<"out", "in">(source, controller))) {
        gr::lora::log_ts("error", "graph", "connect source -> controller failed");
    }

    // ScanSink: receives spectrum (with embedded detections) from ScanController
    auto& sink = graph.emplaceBlock<gr::lora::ScanSink>({});

    if (!ok(graph.connect<"spectrum_out", "spectrum">(controller, sink))) {
        gr::lora::log_ts("error", "graph", "connect controller.spectrum_out -> sink.spectrum failed");
    }
}

} // namespace lora_graph

#endif // GR4_LORA_GRAPH_BUILDER_HPP
