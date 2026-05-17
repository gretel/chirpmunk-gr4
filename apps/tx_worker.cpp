// SPDX-License-Identifier: ISC
//
// tx_worker — implementations for the TX worker-thread helpers declared
// in tx_worker.hpp.  Moved out of lora_trx.cpp so the TX pipeline can be
// tested without a SoapySDR device (see test/qa_lora_trx_worker.cpp).

#include "tx_worker.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <exception>
#include <format>
#include <random>
#include <span>
#include <string>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

#include <gnuradio-4.0/lora/FrameSink.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>
#include <gnuradio-4.0/lora/log.hpp>
#include <gnuradio-4.0/lora/tx_burst_taper.hpp>

using lora_config::TrxConfig;

namespace {

using cf32 = std::complex<float>;

/// Build the IQ for a single TX burst (preamble + payload + tail zeros),
/// then apply the head-pad + raised-cosine taper so the live preamble
/// starts with envelope = 1 and the tail ramps cleanly to zero.
std::vector<cf32> generate_iq(const std::vector<uint8_t>& payload, const TrxConfig& cfg, uint8_t cr_override = 0, uint16_t sync_override = 0, uint16_t preamble_override = 0, uint8_t sf_override = 0, uint32_t bw_override = 0) {
    auto     bw   = bw_override ? bw_override : cfg.bw;
    auto     os   = static_cast<uint8_t>(cfg.rate / static_cast<float>(bw));
    uint8_t  sf   = sf_override ? sf_override : cfg.sf;
    uint32_t sps  = (1u << sf) * os;
    uint8_t  cr   = cr_override ? cr_override : cfg.cr;
    uint16_t sync = sync_override ? sync_override : cfg.sync;
    uint16_t pre  = preamble_override ? preamble_override : cfg.preamble;
    auto     iq   = gr::lora::generate_frame_iq(payload, sf, cr, os, sync, pre, true, sps * 2, 2, bw);
    gr::lora::prependHeadPadAndTaper(iq);
    return iq;
}

int transmit(const std::vector<cf32>& iq, gr::lora::TxQueueSource& source) {
    source.push(iq);
    source.notifyProgress();
    return 0;
}

/// RFC 4122 v4 UUID using a thread-local mt19937_64 seeded from random_device.
/// Uses a process-lifetime RNG instead of FrameSink's per-block _rng so
/// tx_worker stays independent of the RX FrameSink instance.
std::string generate_tx_uuid() {
    static thread_local std::mt19937_64 rng{std::random_device{}()};
    uint64_t                            hi = rng();
    uint64_t                            lo = rng();
    hi                                     = (hi & 0xFFFFFFFFFFFF0FFFULL) | 0x0000000000004000ULL;
    lo                                     = (lo & 0x3FFFFFFFFFFFFFFFULL) | 0x8000000000000000ULL;
    return std::format("{:08x}-{:04x}-{:04x}-{:04x}-{:012x}", static_cast<uint32_t>(hi >> 32), static_cast<uint32_t>((hi >> 16) & 0xFFFFU), static_cast<uint32_t>(hi & 0xFFFFU), static_cast<uint32_t>(lo >> 48), lo & 0x0000FFFFFFFFFFFFULL);
}

/// Monotonically-increasing TX frame counter (thread-safe).
std::atomic<uint32_t> g_tx_seq{0};

/// Build a `lora_frame` CBOR for a successful TX, mirroring FrameSink's
/// RX shape so lora_agg, lora_mon, lora_duckdb and other consumers can
/// treat RX and TX uniformly (disambiguated by the top-level `direction`
/// field).  No `phy` sub-map — DSP state does not exist on the TX side.
std::vector<uint8_t> build_tx_frame_cbor(const std::vector<uint8_t>& payload, uint8_t sf, uint32_t bw, uint8_t cr, uint16_t sync_word) {
    namespace cbor = gr::lora::cbor;
    std::vector<uint8_t> buf;
    buf.reserve(96 + payload.size());

    auto seq   = g_tx_seq.fetch_add(1, std::memory_order_relaxed) + 1;
    auto phash = gr::lora::FrameSink::fnv1a64(payload.data(), payload.size());
    auto uuid  = generate_tx_uuid();

    auto now = std::chrono::system_clock::now();
    auto tt  = std::chrono::system_clock::to_time_t(now);
    char ts_buf[32]{};
    std::strftime(ts_buf, sizeof(ts_buf), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&tt));

    // Layout: type, ts, seq, direction, carrier, payload, payload_len,
    //         payload_hash, crc_valid, is_downchirp, id, decode_label
    cbor::encode_map_begin(buf, 12);
    cbor::kv_text(buf, "type", "lora_frame");
    cbor::kv_text(buf, "ts", ts_buf);
    cbor::kv_uint(buf, "seq", seq);
    cbor::kv_text(buf, "direction", "tx");

    cbor::encode_text(buf, "carrier");
    cbor::encode_map_begin(buf, 5);
    cbor::kv_uint(buf, "sync_word", sync_word);
    cbor::kv_uint(buf, "sf", sf);
    cbor::kv_uint(buf, "bw", bw);
    cbor::kv_uint(buf, "cr", cr);
    // TX knows LDRO deterministically from SF/BW (Semtech rule).
    // Mirror the RX-side carrier shape so lora_agg / lora_duckdb see
    // the same field set on both sides.
    cbor::kv_bool(buf, "ldro_cfg", gr::lora::needs_ldro(sf, bw));

    cbor::kv_bytes(buf, "payload", payload.data(), payload.size());
    cbor::kv_uint(buf, "payload_len", static_cast<uint64_t>(payload.size()));
    cbor::kv_uint(buf, "payload_hash", phash);
    cbor::kv_bool(buf, "crc_valid", true); // we generated it, it's valid by construction
    cbor::kv_bool(buf, "is_downchirp", false);
    cbor::kv_text(buf, "id", uuid);
    cbor::kv_text(buf, "decode_label", "tx");
    return buf;
}

} // namespace

void send_tx_nack(UdpState& udp, const struct sockaddr_storage& sender, uint64_t seq, std::string_view error) noexcept {
    std::vector<uint8_t> rej;
    rej.reserve(80);
    gr::lora::cbor::encode_map_begin(rej, 4);
    gr::lora::cbor::kv_text(rej, "type", "lora_tx_ack");
    gr::lora::cbor::kv_uint(rej, "seq", seq);
    gr::lora::cbor::kv_bool(rej, "ok", false);
    gr::lora::cbor::kv_text(rej, "error", std::string(error));
    udp.sendTo(rej, sender);
}

void handle_tx_request(const gr::lora::cbor::Map& msg, const TrxConfig& cfg, UdpState& udp, const struct sockaddr_storage& sender, gr::lora::TxQueueSource& txSource, gr::lora::SpectrumState* tx_spectrum) {
    using gr::lora::cbor::get_bool_or;
    using gr::lora::cbor::get_uint_or;

    auto payload = gr::lora::cbor::get_bytes(msg, "payload");
    if (payload.empty() || payload.size() > 255) {
        gr::lora::log_ts("error", "lora_trx", "TX invalid payload size %zu", payload.size());
        return;
    }

    auto sf_req  = static_cast<uint8_t>(get_uint_or(msg, "sf", cfg.sf));
    if (sf_req < 7 || sf_req > 12) { sf_req = cfg.sf; }  // clamp to valid SF range

    auto cr      = static_cast<uint8_t>(get_uint_or(msg, "cr", cfg.cr));
    auto sync    = static_cast<uint16_t>(get_uint_or(msg, "sync_word", cfg.sync));
    auto pre     = static_cast<uint16_t>(get_uint_or(msg, "preamble_len", cfg.preamble));
    auto repeat  = static_cast<int>(get_uint_or(msg, "repeat", 1));
    auto gap_ms  = static_cast<int>(get_uint_or(msg, "gap_ms", 1000));
    bool dry_run = get_bool_or(msg, "dry_run", false);
    auto bw_req  = static_cast<uint32_t>(get_uint_or(msg, "bw", 0));
    if (bw_req == 0) { bw_req = cfg.bw; }


    auto iq = generate_iq(payload, cfg, cr, sync, pre, sf_req, bw_req);

    // repeat + gap
    if (repeat > 1) {
        std::vector<cf32> buf;
        auto              gap_samples = static_cast<uint32_t>(cfg.rate * static_cast<float>(gap_ms) / 1000.0f);
        for (int r = 0; r < repeat; r++) {
            buf.insert(buf.end(), iq.begin(), iq.end());
            if (r + 1 < repeat && gap_samples > 0) {
                buf.resize(buf.size() + gap_samples, cf32(0.f, 0.f));
            }
        }
        iq = std::move(buf);
    }

    double                airtime        = static_cast<double>(iq.size()) / static_cast<double>(cfg.rate);
    constexpr std::size_t kMaxHexBytes   = 32;
    auto                  hex_len        = std::min(payload.size(), kMaxHexBytes);
    auto                  hex            = gr::lora::FrameSink::to_hex(std::span<const uint8_t>(payload.data(), hex_len));
    char                  repeat_str[24] = "";
    if (repeat > 1) {
        std::snprintf(repeat_str, sizeof(repeat_str), " repeat=%d", repeat);
    }
    gr::lora::log_ts("info", "lora_trx", "TX payload=%zu sf=%u cr=%u/%u airtime=%.1fms repeat=%d%s", payload.size(), sf_req, 4, cr, airtime * 1000.0, repeat_str, dry_run ? " dry" : "");

    // Push TX IQ to spectrum tap (if connected) for waterfall display
    if (tx_spectrum != nullptr) {
        tx_spectrum->push(iq.data(), iq.size());
    }

    bool ok = true;
    if (!dry_run) {
        ok = (transmit(iq, txSource) == 0);
    }

    // Broadcast a `lora_frame` with direction="tx" to record the actual
    // transmission.  Consumers (lora_agg, lora_mon, lora_duckdb) see RX and
    // TX frames side-by-side on the same wire format.  Filtered broadcast
    // so sync-word subscribers also get matching-sync TX frames (symmetry).
    if (ok && !dry_run) {
        auto tx_frame = build_tx_frame_cbor(payload, sf_req, bw_req, cr, sync);
        udp.broadcast(tx_frame, sync);
    }

    // send ack back to the requesting client
    uint64_t             seq = get_uint_or(msg, "seq", 0);
    std::vector<uint8_t> ack;
    ack.reserve(64);
    gr::lora::cbor::encode_map_begin(ack, 3);
    gr::lora::cbor::kv_text(ack, "type", "lora_tx_ack");
    gr::lora::cbor::kv_uint(ack, "seq", seq);
    gr::lora::cbor::kv_bool(ack, "ok", ok);
    gr::lora::log_ts("debug", "lora_trx", "TX sending ack ok=%d seq=%" PRIu64 " (%zu CBOR bytes)", ok, seq, ack.size());
    udp.sendTo(ack, sender);
}

void process_tx_request(const TxRequest& req, const TrxConfig& cfg, UdpState& udp, gr::lora::TxQueueSource& tx_source, std::atomic<bool>& channel_busy, gr::lora::SpectrumState* tx_spectrum) noexcept {
    // Defensive seq extract (no-throw) so the catch handler can NACK
    // the correct sequence number even when CBOR parse fails later.
    uint64_t seq = 0;
    if (auto it = req.msg.find("seq"); it != req.msg.end()) {
        if (auto* p = std::get_if<uint64_t>(&it->second)) {
            seq = *p;
        }
    }
    try {
        if (cfg.lbt) {
            if (channel_busy.load(std::memory_order_acquire)) {
                gr::lora::log_ts("debug", "lora_trx", "TX LBT: channel busy, waiting up to %ums (seq=%" PRIu64 ")", cfg.lbt_timeout_ms, seq);
            }
            auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(cfg.lbt_timeout_ms);
            while (channel_busy.load(std::memory_order_acquire)) {
                if (std::chrono::steady_clock::now() >= deadline) {
                    gr::lora::log_ts("warn ", "lora_trx", "LBT timeout (channel busy >%ums), rejecting seq=%" PRIu64, cfg.lbt_timeout_ms, seq);
                    send_tx_nack(udp, req.sender, seq, "channel_busy");
                    return;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
        }
        gr::lora::log_ts("debug", "lora_trx", "TX calling handle_tx_request (seq=%" PRIu64 ")", seq);
        handle_tx_request(req.msg, cfg, udp, req.sender, tx_source, tx_spectrum);
    } catch (const std::exception& e) {
        gr::lora::log_ts("error", "lora_trx", "TX request failed: %s (seq=%" PRIu64 ")", e.what(), seq);
        send_tx_nack(udp, req.sender, seq, "internal");
    } catch (...) {
        gr::lora::log_ts("error", "lora_trx", "TX request failed (unknown, seq=%" PRIu64 ")", seq);
        send_tx_nack(udp, req.sender, seq, "internal");
    }
}
