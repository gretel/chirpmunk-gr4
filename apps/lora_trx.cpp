// SPDX-License-Identifier: ISC
//
// lora_trx: full-duplex LoRa transceiver using native GR4 SoapySDR blocks.
//
// 1-ch RX: SoapySimpleSource -> [Splitter] -> MultiSfDecoder (per BW) -> FrameSink
// 2-ch RX: SoapyDualSource -+-> [Splitter] -> MultiSfDecoder (per BW) -> FrameSink
//                            +-> [Splitter] -> MultiSfDecoder (per BW) -> FrameSink
// TX: bounded request queue with dedicated worker thread; LBT defers TX until channel clear
//
// MultiSfDecoder decodes all SFs (7-12) simultaneously on each channel.
// Full-duplex TX in both modes: RX runs continuously while TX uses an
// ephemeral graph with a separate device handle.
//
// All I/O is CBOR over UDP. RX frames fan out to all connected clients.
// TX requests (type: "lora_tx") are processed and acked.

#include "config.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cinttypes>
#include <complex>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

#include <gnuradio-4.0/Graph.hpp>
#include <gnuradio-4.0/Scheduler.hpp>
#include <gnuradio-4.0/Tensor.hpp>
#include <gnuradio-4.0/testing/NullSources.hpp>
#include <gnuradio-4.0/testing/TagMonitors.hpp>

#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>
#include <gnuradio-4.0/lora/tx_burst_taper.hpp>

// graph_builder.hpp includes SoapySource.hpp which defines detail::equalWithinOnePercent
// — must come before SoapySink.hpp which uses it
#include "graph_builder.hpp"
#include "tx_worker.hpp"
#include "udp_state.hpp"
#include <gnuradio-4.0/sdr/SoapySink.hpp>

#include <gnuradio-4.0/lora/FrameSink.hpp>
#include <gnuradio-4.0/lora/TxQueueSource.hpp>
#include <gnuradio-4.0/lora/algorithm/Telemetry.hpp>
#include <gnuradio-4.0/lora/cbor.hpp>
#include <gnuradio-4.0/lora/log.hpp>

using namespace lora_config;
using namespace lora_graph;
using namespace std::string_literals;

namespace {

using lora_apps::UdpState;

// --- TX ---

using cf32 = std::complex<float>;

// Persistent TX graph: stays alive for the process lifetime.
//
// Architecture (dual-channel device, e.g. B210):
//   TxQueueSource(out0=IQ, out1=zeros) -> SoapyDualSink<cf32>(in#0, in#1)
//   B210 requires symmetric 2T2R — both TX channels open even though only
//   one carries IQ.  The unused channel receives zeros from out1.
//
// Architecture (single-channel device, e.g. PlutoPAPR):
//   TxQueueSource(out0=IQ)    -> SoapySimpleSink<cf32>(in)  [channel_offset]
//   TxQueueSource(out1=zeros) -> NullSink<cf32>(in)
//
// build_tx_graph receives the TX channel count from log_hardware_info and
// picks the matching SoapySink template instantiation.  TxQueueSource always
// emits IQ on out0 and zeros on out1 in lockstep; in the single-channel case
// out1 is drained into a NullSink so the scheduler has a valid consumer for
// every port.
//
// The scheduler and device handle are created once at startup (before RX
// starts) so reinitDevice() never races the RX stream.  TX requests push IQ
// into TxQueueSource::push() and call notifyProgress() to wake the
// singleThreadedBlocking scheduler.
struct TxGraph {
    gr::lora::TxQueueSource*                                                       source{nullptr};
    gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking>* sched{nullptr};
    std::thread                                                                    thread{};
};

std::unique_ptr<gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking>> build_tx_graph(gr::lora::TxQueueSource*& source_out, const TrxConfig& cfg, std::size_t nTxChannels) {
    if (nTxChannels == 0) {
        gr::lora::log_ts("error", "lora_trx", "TX probe: device '%s' reports 0 TX channels", cfg.device.c_str());
        return nullptr;
    }

    if (cfg.tx_channel >= nTxChannels) {
        gr::lora::log_ts("error", "lora_trx", "tx_channel %u but device has only %zu TX channel(s)", cfg.tx_channel, nTxChannels);
        return nullptr;
    }

    gr::Graph graph;
    graph.emplaceBlock<gr::lora::TxQueueSource>();

    // Helper: locate TxQueueSource via the graph's block list.  emplaceBlock
    // returns a reference that becomes dangling after exchange(std::move(graph))
    // so we cannot keep it directly.  The lookup runs once after all
    // emplaceBlock calls are done.
    auto find_tx_queue_source = [&graph]() -> gr::lora::TxQueueSource* {
        for (auto& blk : graph.blocks()) {
            if (blk->typeName().find("TxQueueSource") != std::string_view::npos) {
                return static_cast<gr::lora::TxQueueSource*>(blk->raw());
            }
        }
        return nullptr;
    };

    // B210 requires symmetric TX/RX channel counts (2R1T not supported).
    // When dual-channel RX is active, we must use dual-channel TX (2T2R mode)
    // even though only one TX channel carries IQ — the other gets zeros.
    // For single-channel devices (PAPR, loopback), use SoapySimpleSink.
    const bool dualTx = (nTxChannels >= 2);

    gr::lora::log_ts("debug", "lora_trx", "tx sink    %s (%zu TX channel%s on %s)", dualTx ? "dual-channel" : "single-channel", nTxChannels, nTxChannels == 1 ? "" : "s", cfg.device.c_str());

    gr::lora::TxQueueSource* src_ptr = nullptr;

    if (dualTx) {
        auto& sink = graph.emplaceBlock<gr::blocks::sdr::SoapyDualSink<cf32>>({
            {"device", cfg.device},
            {"device_parameter", cfg.device_param},
            {"sample_rate", cfg.rate},
            {"frequency", std::vector<double>{cfg.freq, cfg.freq}},
            {"tx_gains", std::vector<double>{cfg.gain_tx, 0.0}},
            {"num_channels", gr::Size_t{2}},
            {"tx_antennae", cfg.tx_antenna.empty() ? std::vector<std::string>{} : std::vector<std::string>{cfg.tx_antenna}},
            {"clock_source", cfg.clock},
            {"master_clock_rate", 24.0e6},
            {"timed_tx", true},
            {"wait_burst_ack", true},
            {"max_underflow_count", gr::Size_t{0}},
        });

        src_ptr = find_tx_queue_source();
        if (!src_ptr) {
            gr::lora::log_ts("error", "lora_trx", "TxQueueSource not found in TX graph");
            return nullptr;
        }
        auto& src = *src_ptr;

        // Route IQ to the correct soapy channel; zeros go to the other.
        const bool tx_on_ch1 = (cfg.tx_channel == 1);
        if (!tx_on_ch1) {
            std::ignore = graph.connect(src, "out0"s, sink, "in#0"s);
            std::ignore = graph.connect(src, "out1"s, sink, "in#1"s);
        } else {
            std::ignore = graph.connect(src, "out1"s, sink, "in#0"s);
            std::ignore = graph.connect(src, "out0"s, sink, "in#1"s);
        }
    } else {
        auto& sink = graph.emplaceBlock<gr::blocks::sdr::SoapySimpleSink<cf32>>({
            {"device", cfg.device},
            {"device_parameter", cfg.device_param},
            {"sample_rate", cfg.rate},
            {"frequency", std::vector<double>{cfg.freq}},
            {"tx_gains", std::vector<double>{cfg.gain_tx}},
            {"num_channels", gr::Size_t{1}},
            {"channel_offset", static_cast<gr::Size_t>(cfg.tx_channel)},
            {"tx_antennae", cfg.tx_antenna.empty() ? std::vector<std::string>{} : std::vector<std::string>{cfg.tx_antenna}},
            {"clock_source", cfg.clock},
            {"master_clock_rate", 0.0},
            {"timed_tx", true},
            {"wait_burst_ack", true},
            {"max_underflow_count", gr::Size_t{0}},
        });

        auto& nullSink = graph.emplaceBlock<gr::testing::NullSink<cf32>>();

        src_ptr = find_tx_queue_source();
        if (!src_ptr) {
            gr::lora::log_ts("error", "lora_trx", "TxQueueSource not found in TX graph");
            return nullptr;
        }
        auto& src = *src_ptr;

        std::ignore = graph.connect(src, "out0"s, sink, "in"s);
        std::ignore = graph.connect(src, "out1"s, nullSink, "in"s);
    }

    auto sched = std::make_unique<gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking>>();
    // TX scheduler: sleep quickly when idle (TX is long-idle between requests).
    // timeout_inactivity_count gates BOTH the sleep trigger AND the watchdog print
    // threshold (`nWarnings >= timeOut_count` in runWatchDog).  Setting it to
    // UINT32_MAX prevents the worker from ever sleeping; 10U gives fast sleep.
    // Push watchdog_timeout up to ~1 hour so its spam fires once per hour.
    // watchdog_timeout is the sleep_for interval inside runWatchDog and is
    // independent of the worker thread's `timeout_ms` (which controls the
    // waitUntilChanged sleep duration via _newDataReady-style notifications).
    sched->timeout_inactivity_count = 10U;
    sched->watchdog_timeout         = 3'600'000U; // 1 hour
    if (auto ret = sched->exchange(std::move(graph)); !ret) {
        gr::lora::log_ts("error", "lora_trx", "TX scheduler init failed");
        return nullptr;
    }

    // src_ptr was captured pre-exchange; the TxQueueSource object is now owned
    // by the scheduler via shared_ptr but the raw pointer remains valid for the
    // process lifetime. No need to re-scan sched->blocks() post-exchange.
    source_out = src_ptr;

    // (TX chain + antenna are printed as part of the startup banner in main.)
    return sched;
}

// --- TX request queue for LBT ---

/// Bounded FIFO queue for TX requests.  Thread-safe (mutex + condvar).
/// The UDP loop pushes; a dedicated TX worker thread pops and processes.
class TxRequestQueue {
public:
    explicit TxRequestQueue(std::size_t capacity) : _capacity(capacity) {}

    /// Push a request.  Returns false if queue is full (caller should reject).
    bool push(TxRequest req) {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_queue.size() >= _capacity) {
            return false;
        }
        _queue.push(std::move(req));
        _cv.notify_one();
        return true;
    }

    /// Pop a request, blocking until one is available or stop is requested.
    /// Returns std::nullopt on stop.
    std::optional<TxRequest> pop() {
        std::unique_lock<std::mutex> lock(_mutex);
        _cv.wait(lock, [this] { return !_queue.empty() || _stop; });
        if (_stop && _queue.empty()) {
            return std::nullopt;
        }
        auto req = std::move(_queue.front());
        _queue.pop();
        return req;
    }

    void request_stop() {
        std::lock_guard<std::mutex> lock(_mutex);
        _stop = true;
        _cv.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(_mutex);
        return _queue.size();
    }

private:
    mutable std::mutex      _mutex;
    std::condition_variable _cv;
    std::queue<TxRequest>   _queue;
    std::size_t             _capacity;
    bool                    _stop{false};
};

// Runtime PHY reconfiguration (the old `lora_config` CBOR handler) was
// removed. PHY parameters (sf / bw / sync_word / preamble / freq /
// rx_gain) are configuration-only now: edit apps/config.toml and restart
// lora_trx.  The previous API was designed for a single-chain world and
// its broadcast-to-all-decoders semantics are incompatible with the
// current multi-chain + promiscuous + sf_set architecture (changing one
// field would clobber unrelated chains).  HW A/B matrix tests cover any
// configuration they need at startup via multi-BW + multi-chain +
// SF7-12 sweep + promiscuous RX — no runtime reconfig needed.

} // namespace

int main(int argc, char* argv[]) {
    // Install the structured terminate handler before anything that can
    // throw — UHD/libusb errors raised from the SoapySDR IO thread bypass
    // our scheduler-thread try/catch and only land here.
    lora_apps::install_terminate_handler("lora_trx");

    std::string config_path;
    std::string log_level;
    int         rc = parse_args(argc, argv, config_path, log_level);
    if (rc != 0) {
        return rc == 2 ? 0 : 1; // 2 = --help/--version (clean exit)
    }

    auto configs = load_config(config_path, log_level);
    if (configs.empty()) {
        return 1;
    }

    // Use first set for now (multi-set support is future work)
    TrxConfig cfg = std::move(configs[0]);

    if (cfg.rx_channels.empty()) {
        gr::lora::log_ts("error", "lora_trx", "rx_channel must specify at least one channel");
        return 1;
    }
    // Validate no duplicate SoapySDR channel indices
    {
        std::vector<uint32_t> seen;
        for (auto ch : cfg.rx_channels) {
            if (std::ranges::find(seen, ch) != seen.end()) {
                gr::lora::log_ts("error", "lora_trx", "duplicate rx_channel %u — each SoapySDR channel can only appear once", ch);
                return 1;
            }
            seen.push_back(ch);
        }
    }

    auto& stop_flag = lora_apps::install_signal_handler();

    auto os = static_cast<uint8_t>(cfg.rate / static_cast<float>(cfg.bw));

    lora_apps::log_version_banner("lora_trx", GIT_REV);
    {
        // config line: path + active set name (bracketed for scannability).
        // Drop the "(1 set)" qualifier when there's only one — redundant.
        if (configs.size() > 1) {
            gr::lora::log_ts("info ", "lora_trx", "config     %s [%s] (%zu sets, active)", config_path.c_str(), cfg.name.c_str(), configs.size());
        } else {
            gr::lora::log_ts("info ", "lora_trx", "config     %s [%s]", config_path.c_str(), cfg.name.c_str());
        }
    }

    auto hw_info       = lora_apps::log_hardware_info("lora_trx", cfg.device, cfg.device_param);
    auto device_serial = hw_info.serial;

    // Human-facing banner: device, radio, rx, tx, telemetry, ready.
    // Plumbing details (build timestamp, driver kwargs, os factor, preamble,
    // graph internals) are logged separately at debug level.
    {
        // device: driver + serial + clock source.  Serial "?" when enumerate
        // returned nothing (soapy-loopback, sim devices).
        gr::lora::log_ts("info ", "lora_trx", "device     %s %s (clock: %s)", cfg.device.c_str(), device_serial.empty() ? "?" : device_serial.c_str(), cfg.clock.empty() ? "default" : cfg.clock.c_str());

        // radio: freq + modulation params.  BW shown as kHz (human-friendly).
        gr::lora::log_ts("info ", "lora_trx", "radio      %.3f MHz  SF %u  BW %.1f kHz  CR 4/%u  sync 0x%02X", cfg.freq / 1e6, cfg.sf, cfg.bw / 1e3, 4u + cfg.cr, cfg.sync);

        // Sampling plumbing → debug.
        gr::lora::log_ts("debug", "lora_trx", "sample     rate %.0f kS/s  os %u  preamble %u", static_cast<double>(cfg.rate) / 1e3, os, cfg.preamble);

        // rx chains: ch/antenna pairs + gain.
        std::string rx_ch_str;
        for (std::size_t i = 0; i < cfg.rx_channels.size(); i++) {
            if (!rx_ch_str.empty()) {
                rx_ch_str += "  ";
            }
            rx_ch_str += "ch";
            rx_ch_str += std::to_string(cfg.rx_channels[i]);
            if (i < cfg.rx_antenna.size() && !cfg.rx_antenna[i].empty()) {
                rx_ch_str += "/";
                rx_ch_str += cfg.rx_antenna[i];
            }
        }
        gr::lora::log_ts("info ", "lora_trx", "rx chains  %s  @ %.0f dB", rx_ch_str.c_str(), cfg.gain_rx);

        // tx chain: single entry + gain + LBT summary.
        std::string tx_label = "ch";
        tx_label += std::to_string(cfg.tx_channel);
        if (!cfg.tx_antenna.empty()) {
            tx_label += "/";
            tx_label += cfg.tx_antenna;
        }
        if (cfg.lbt) {
            gr::lora::log_ts("info ", "lora_trx", "tx chain   %s  @ %.0f dB   LBT: %u ms, queue %u", tx_label.c_str(), cfg.gain_tx, cfg.lbt_timeout_ms, cfg.tx_queue_depth);
        } else {
            gr::lora::log_ts("info ", "lora_trx", "tx chain   %s  @ %.0f dB   LBT: off", tx_label.c_str(), cfg.gain_tx);
        }
    }
    // rx chains banner (always emit; multi-chain users especially need this)
    {
        std::string dec_str;
        for (std::size_t i = 0; i < cfg.rx_chains.size(); i++) {
            const auto& dc = cfg.rx_chains[i];
            if (i > 0) {
                dec_str += "  ";
            }

            // SF subset label: "SF7-12" for default sweep, "SFN" pinned,
            // "SF{8,11}" for non-contiguous lists.
            std::string sf_label;
            if (dc.sf_set.empty()) {
                sf_label = "SF7-12";
            } else if (dc.sf_set.size() == 1) {
                sf_label = "SF" + std::to_string(dc.sf_set[0]);
            } else {
                sf_label = "SF{";
                for (std::size_t k = 0; k < dc.sf_set.size(); ++k) {
                    if (k > 0) {
                        sf_label += ",";
                    }
                    sf_label += std::to_string(dc.sf_set[k]);
                }
                sf_label += "}";
            }

            // sync_word label: "any" in promiscuous mode, "0xXX" otherwise.
            char sw_label[16];
            if (dc.sync_word.has_value()) {
                std::snprintf(sw_label, sizeof(sw_label), "0x%02X", *dc.sync_word);
            } else {
                std::snprintf(sw_label, sizeof(sw_label), "any");
            }

            dec_str += dc.label;
            dec_str += "(";
            dec_str += sf_label;
            dec_str += "/";
            dec_str += sw_label;
            dec_str += ")";
        }
        std::string bw_str;
        for (std::size_t i = 0; i < cfg.rx_bandwidths.size(); i++) {
            if (i > 0) {
                bw_str += ",";
            }
            if (cfg.rx_bandwidths[i] >= 1000) {
                bw_str += std::to_string(cfg.rx_bandwidths[i] / 1000) + "k";
            } else {
                bw_str += std::to_string(cfg.rx_bandwidths[i]);
            }
        }
        gr::lora::log_ts("info ", "lora_trx", "rx chains  [%s] x BW [%s]  = %zu decoder(s) per radio", dec_str.c_str(), bw_str.c_str(), cfg.rx_bandwidths.size() * cfg.rx_chains.size());
    }
    // telemetry line is emitted AFTER UDP socket binds (below) so the bind
    // failure path aborts before we promise an address.
    gr::lora::log_ts("debug", "lora_trx", "debug output enabled");

    // --- Bind UDP socket ---
    UdpState udp;
    udp.fd = lora_apps::create_udp_socket(cfg.listen, cfg.port);
    if (udp.fd < 0) {
        return 1;
    }

    // Human-facing telemetry endpoint + heartbeat interval in one line.
    if (cfg.status_interval > 0) {
        gr::lora::log_ts("info ", "lora_trx", "telemetry  udp://%s:%u  (heartbeat %u s)", cfg.listen.c_str(), cfg.port, cfg.status_interval);
    } else {
        gr::lora::log_ts("info ", "lora_trx", "telemetry  udp://%s:%u", cfg.listen.c_str(), cfg.port);
    }

    // --- Shared status for periodic heartbeat ---
    SharedStatus shared_status;

    // --- Frame callback: fan out RX frames to all clients + update status ---
    // sync_word is the per-chain RX filter (from [[trx.receive.chain]]), NOT
    // the TX preset — multi-chain decode uses different sync_words on the same
    // RF tune and each frame must fan out to the correct subscriber group.
    auto frame_callback = [&udp, &shared_status](const std::vector<uint8_t>& cbor_frame, bool crc_valid, uint16_t sync_word) {
        udp.broadcast(cbor_frame, sync_word);
        shared_status.frame_count.fetch_add(1, std::memory_order_relaxed);
        if (crc_valid) {
            shared_status.crc_ok.fetch_add(1, std::memory_order_relaxed);
        } else {
            shared_status.crc_fail.fetch_add(1, std::memory_order_relaxed);
        }
    };

    // --- Build persistent TX graph ---
    // SoapySink stays open for the process lifetime; TxQueueSource receives
    // bursts from the UDP loop thread via push() + notifyProgress().
    // The TX thread is launched AFTER the RX scheduler (see below) so that
    // DeviceRegistry coordinates both streams before either activates.
    //
    // The TX graph is skipped when cfg.enable_tx = false (RX-only mode).
    gr::lora::TxQueueSource*                                                                       tx_source_ptr = nullptr;
    std::unique_ptr<gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking>> tx_sched;
    std::thread                                                                                    tx_thread;

    if (!cfg.enable_tx) {
        gr::lora::log_ts("info ", "lora_trx", "TX disabled by config (enable_tx = false): RX-only mode");
    } else {
        tx_sched = build_tx_graph(tx_source_ptr, cfg, hw_info.tx_channels);
        if (!tx_sched || tx_source_ptr == nullptr) {
            gr::lora::log_ts("warn ", "lora_trx", "failed to build TX graph — continuing RX-only");
        }
        // TX thread is launched AFTER the RX scheduler starts (below).
        // This ensures the RX SoapySource calls DeviceRegistry::findOrCreate
        // before the TX SoapySink calls registerActivation — so both streams
        // are set up before either is activated.  Without this ordering,
        // TX would activate alone (pendingUsers→0) and UHD would reject
        // the subsequent RX setupStream ("2 RX 1 TX not possible" on B210).
    }

    // --- Spectrum taps for waterfall display ---
    auto spectrum         = std::make_shared<gr::lora::SpectrumState>();
    spectrum->sample_rate = cfg.rate;
    spectrum->center_freq = cfg.freq;
    spectrum->init();

    // TX spectrum: fed by handle_tx_request before transmit
    auto tx_spectrum         = std::make_shared<gr::lora::SpectrumState>();
    tx_spectrum->sample_rate = cfg.rate;
    tx_spectrum->center_freq = cfg.freq;
    tx_spectrum->init();

    // singleThreadedBlocking doesn't use the pool; shrink to avoid idle thread CPU waste
    gr::thread_pool::Manager::defaultCpuPool()->setThreadBounds(1, 1);

    // --- LBT: channel-busy flag updated by CAD in the RX graph ---
    std::atomic<bool> channel_busy{false};

    // --- Build and start RX graph in a background thread ---
    gr::Graph                rx_graph;
    auto                     rx_telemetry = [&udp](const gr::property_map& evt) { udp.broadcast_all(gr::lora::telemetry::encode(evt)); };
    std::atomic<gr::Size_t>* overflow_ptr = build_rx_graph(rx_graph, cfg, frame_callback, spectrum, &channel_busy, std::move(rx_telemetry));

    gr::scheduler::Simple<gr::scheduler::ExecutionPolicy::singleThreadedBlocking> rx_sched;
    rx_sched.timeout_inactivity_count = 10U; // log after 10s of no progress (SoapySource IO thread drives progress)
    // NB: a `watchdog_max_warnings` knob exists on newer gnuradio4 master
    // (post-4ab98c4) — when this submodule is bumped, set it here so a
    // multi-minute pipeline stall (wedged libusb, SoapyUHD reactivate
    // stuck) self-aborts via the scheduler's ERROR transition. Until
    // then, host-side detection lives in the harness's assert_alive
    // poll on the lora_trx process group.
    if (auto ret = rx_sched.exchange(std::move(rx_graph)); !ret) {
        gr::lora::log_ts("error", "lora_trx", "RX scheduler init failed");
        if (tx_sched) {
            tx_sched->requestStop();
            if (tx_thread.joinable()) {
                tx_thread.join();
            }
        }
        ::close(udp.fd);
        return 1;
    }

    // Locate RX blocks in the live scheduler graph for runtime reconfig.
    // After exchange(), the original emplaceBlock references are dangling —
    // we find them by scanning typeName().
    auto rx_blocks = find_rx_blocks(rx_sched.blocks());
    set_device_serial(rx_sched.blocks(), device_serial);

    // TX blocks intentionally not tracked for runtime reconfiguration —
    // tx_gain / tx_freq are baked at graph build from config.toml.
    gr::lora::log_ts("debug", "lora_trx", "rx graph   %zu MultiSfDecoder (SF7-12), soapy=%s", rx_blocks.multisf_decoders.size(), rx_blocks.soapy_source ? "yes" : "no");

    // Launch RX scheduler FIRST so its SoapySource::findOrCreate registers
    // on the DeviceRegistry before TX's registerActivation fires.  This
    // ensures pendingUsers >= 2 and BOTH streams are set up before either
    // is activated — required by B210 which only supports symmetric 2T2R
    // (not 2R1T).  A 100ms delay before TX launch gives the RX thread time
    // to call findOrCreate (nearly instant) before the TX thread does.
    std::atomic<bool> rx_done{false};
    auto              rx_start_time = std::chrono::steady_clock::now();
    std::thread       rx_thread([&rx_sched, &rx_done, &shared_status, rx_start_time, overflow_ptr]() {
        // Exception safety: any std::exception escaping runAndWait()
        // would otherwise trigger std::terminate and bypass the TX-chain
        // teardown path.  Catch and log so the scheduler stop sequence
        // can run normally.
        try {
            auto ret = rx_sched.runAndWait();
            // Sync final overflow count from source block
            if (overflow_ptr) {
                shared_status.overflow_count.store(*overflow_ptr, std::memory_order_relaxed);
            }
            if (!ret.has_value()) {
                auto uptime = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now() - rx_start_time).count();
                auto err    = std::format("{}", ret.error());
                gr::lora::log_ts("warn ", "lora_trx", "RX scheduler stopped: %s", err.c_str());
                gr::lora::log_ts("info ", "lora_trx", "uptime %llds  frames %u (crc_ok %u crc_fail %u)  overflows %" PRIu64, static_cast<long long>(uptime), shared_status.frame_count.load(), shared_status.crc_ok.load(), shared_status.crc_fail.load(), shared_status.overflow_count.load());
                if (err.find("OVERFLOW") != std::string::npos) {
                    gr::lora::log_ts("info ", "lora_trx",
                        "hint: sustained USB buffer overflow — "
                        "try increasing num_recv_frames (e.g. device_param="
                        "\"num_recv_frames=128\") or reducing sample rate");
                }
                if (err.find("consecutive errors") != std::string::npos) {
                    gr::lora::log_ts("info ", "lora_trx",
                        "hint: SDR device may be unresponsive — "
                        "check USB connection and device health");
                }
                if (err.find("watchdog stall") != std::string::npos) {
                    gr::lora::log_ts("info ", "lora_trx",
                        "hint: RX pipeline stalled — "
                        "no data processed for extended period");
                }
            }
        } catch (const std::exception& e) {
            gr::lora::log_ts("error", "lora_trx", "RX scheduler crashed: %s — safety reset will run on exit", e.what());
        } catch (...) {
            gr::lora::log_ts("error", "lora_trx", "RX scheduler crashed (unknown) — safety reset will run on exit");
        }
        rx_done.store(true, std::memory_order_relaxed);
    });

    gr::lora::log_ts("info ", "lora_trx", "ready      %zu decoder%s (SF7-12) on %zu RX chain%s — Ctrl+C to stop", rx_blocks.multisf_decoders.size(), rx_blocks.multisf_decoders.size() == 1 ? "" : "s", cfg.rx_channels.size(), cfg.rx_channels.size() == 1 ? "" : "s");

    // Now launch TX thread — RX's findOrCreate has already registered on the
    // DeviceRegistry (it's the first thing reinitDevice does, before any
    // apply* calls).  A brief delay ensures the RX thread has entered
    // reinitDevice before the TX thread calls registerActivation.
    if (tx_sched && tx_source_ptr) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        tx_thread = std::thread([&tx_sched]() {
            try {
                auto ret = tx_sched->runAndWait();
                if (!ret.has_value()) {
                    gr::lora::log_ts("warn ", "lora_trx", "TX scheduler stopped: %s", std::format("{}", ret.error()).c_str());
                }
            } catch (const std::exception& e) {
                gr::lora::log_ts("error", "lora_trx", "TX scheduler crashed: %s — continuing RX-only", e.what());
            } catch (...) {
                gr::lora::log_ts("error", "lora_trx", "TX scheduler crashed (unknown) — continuing RX-only");
            }
        });
    }

    // --- Pre-build config CBOR (sent once on subscribe) ---
    auto config_cbor = build_config_cbor(cfg, device_serial);

    // --- TX request queue + worker thread (replaces tx_busy + io pool dispatch) ---
    TxRequestQueue tx_queue(cfg.tx_queue_depth);
    std::thread    tx_worker;

    if (tx_source_ptr != nullptr) {
        gr::lora::TxQueueSource& tx_source = *tx_source_ptr;
        tx_worker                          = std::thread([&tx_queue, &cfg, &udp, &tx_source, &channel_busy, tx_spectrum_ref = tx_spectrum]() {
            while (auto req = tx_queue.pop()) {
                process_tx_request(*req, cfg, udp, tx_source, channel_busy, tx_spectrum_ref.get());
            }
        });
    }

    // --- Main loop: recv UDP datagrams, dispatch TX requests ---
    std::vector<uint8_t> recv_buf(65536);
    auto                 last_status = std::chrono::steady_clock::now();

    while (!stop_flag.load(std::memory_order_relaxed) && !rx_done.load(std::memory_order_relaxed)) {
        struct sockaddr_storage sender{};
        socklen_t               slen = sizeof(sender);
        auto                    n    = ::recvfrom(udp.fd, recv_buf.data(), recv_buf.size(), 0, reinterpret_cast<struct sockaddr*>(&sender), &slen);

        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                // Periodic status heartbeat
                if (cfg.status_interval > 0) {
                    auto now     = std::chrono::steady_clock::now();
                    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - last_status).count();
                    if (elapsed >= static_cast<int64_t>(cfg.status_interval)) {
                        // Sync overflow counter from source block
                        if (overflow_ptr) {
                            shared_status.overflow_count.store(*overflow_ptr, std::memory_order_relaxed);
                        }
                        auto status_msg = build_status_cbor(cfg, shared_status, GIT_REV, device_serial);
                        udp.broadcast_all(status_msg);
                        last_status = now;
                    }
                }
                // Spectrum FFT: compute and broadcast if new data available
                if (spectrum->compute()) {
                    auto spec_msg = build_spectrum_cbor(*spectrum, "spectrum");
                    udp.broadcast_all(spec_msg);
                }
                if (tx_spectrum->compute()) {
                    auto spec_msg = build_spectrum_cbor(*tx_spectrum, "spectrum_tx");
                    udp.broadcast_all(spec_msg);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            break;
        }

        if (n == 0) {
            udp.subscribe(sender);
            udp.sendTo(config_cbor, sender);
            continue;
        }

        // Try to decode as CBOR message
        try {
            auto msg  = gr::lora::cbor::decode_map(std::span<const uint8_t>(recv_buf.data(), static_cast<std::size_t>(n)));
            auto type = gr::lora::cbor::get_text_or(msg, "type", "");
            if (type == "lora_tx") {
                udp.subscribe(sender);
                if (tx_source_ptr == nullptr) {
                    // TX disabled (enable_tx=false) —
                    // reject with an explicit error so the client does not
                    // retry against a phantom queue.
                    uint64_t seq = gr::lora::cbor::get_uint_or(msg, "seq", 0);
                    send_tx_nack(udp, sender, seq, "tx_disabled");
                } else if (!tx_queue.push({msg, sender})) {
                    uint64_t seq = gr::lora::cbor::get_uint_or(msg, "seq", 0);
                    send_tx_nack(udp, sender, seq, "tx_queue_full");
                }
            } else if (type == "lora_config") {
                // Runtime PHY reconfig was removed — config is startup-only.
                udp.subscribe(sender);
                namespace cbor = gr::lora::cbor;
                std::vector<uint8_t> nack;
                nack.reserve(80);
                cbor::encode_map_begin(nack, 3);
                cbor::kv_text(nack, "type", "lora_config_ack");
                cbor::kv_bool(nack, "ok", false);
                cbor::kv_text(nack, "error", "runtime_phy_reconfig_removed");
                udp.sendTo(nack, sender);
                gr::lora::log_ts("warn ", "lora_trx",
                    "lora_config: runtime PHY reconfig removed; "
                    "edit config.toml and restart to change SF/BW/sync/freq/gain");
            } else if (type == "subscribe") {
                // Optional sync_word filter — array of uint16 or single uint
                std::vector<uint16_t> filter;
                try {
                    auto arr = gr::lora::cbor::get_uint_array_or(msg, "sync_word");
                    for (auto val : arr) {
                        filter.push_back(static_cast<uint16_t>(val));
                    }
                } catch (const gr::lora::cbor::DecodeError&) {
                    // Fallback: single uint value
                    auto sw = gr::lora::cbor::get_uint_or(msg, "sync_word", 0);
                    if (sw > 0) {
                        filter.push_back(static_cast<uint16_t>(sw));
                    }
                }
                udp.subscribe(sender, std::move(filter));
                udp.sendTo(config_cbor, sender);
            } else {
                udp.subscribe(sender);
                udp.sendTo(config_cbor, sender);
            }
        } catch (const gr::lora::cbor::DecodeError&) {
            // Not CBOR — bare registration datagram (e.g. "sub")
            udp.subscribe(sender);
            udp.sendTo(config_cbor, sender);
        }
    }

    // --- Shutdown ---
    // GR4 scheduler + SoapySDR teardown can hang indefinitely (known upstream
    // issue). Request stop on all schedulers, give threads 2s to finish, then
    // force-exit. Order: signal all stops first, then join with timeout.
    gr::lora::log_ts("info ", "lora_trx", "stopping");
    if (!rx_done.load(std::memory_order_relaxed)) {
        rx_sched.requestStop();
    }
    tx_queue.request_stop();
    if (tx_sched) {
        tx_sched->requestStop();
        tx_source_ptr->notifyProgress();
    }

    auto deadline   = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    auto timed_join = [deadline](std::thread& t) {
        if (!t.joinable()) {
            return;
        }
        while (std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (t.joinable()) {
            t.detach();
        }
    };
    timed_join(rx_thread);
    if (tx_worker.joinable()) {
        tx_worker.join();
    }
    timed_join(tx_thread);

    ::close(udp.fd);

    gr::lora::log_ts("info ", "lora_trx", "stopped");
    std::quick_exit(0);
}
