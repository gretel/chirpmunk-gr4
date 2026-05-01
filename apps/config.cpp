// SPDX-License-Identifier: ISC
//
// Configuration loading and CBOR serialisation — implementation.

#include "config.hpp"

#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <cstdio>
#include <map>
#include <ranges>
#include <string>

#include <toml++/toml.hpp>

#include <gnuradio-4.0/lora/log.hpp>

namespace lora_config {

// --- Property map utilities (internal) ---

// TOML has only int64_t, double, bool, string. GR4 blocks need exact types
// (uint8_t, float, etc.) for applyStagedParameters. We store floats as float
// (not double) since most block properties use float, and narrow integers to
// the smallest fitting unsigned type.
gr::property_map toml_to_property_map(const toml::table& tbl) {
    gr::property_map props;
    for (const auto& [key, val] : tbl) {
        std::pmr::string k(key.str());
        if (val.is_floating_point()) {
            props[k] = static_cast<float>(val.as_floating_point()->get());
        } else if (val.is_integer()) {
            auto v = val.as_integer()->get();
            if (v >= 0 && v <= 255) {
                props[k] = static_cast<uint8_t>(v);
            } else if (v >= 0 && v <= 65535) {
                props[k] = static_cast<uint16_t>(v);
            } else if (v >= 0) {
                props[k] = static_cast<uint32_t>(v);
            } else {
                props[k] = static_cast<int32_t>(v);
            }
        } else if (val.is_boolean()) {
            props[k] = val.as_boolean()->get();
        } else if (val.is_string()) {
            props[k] = std::string(val.as_string()->get());
        }
    }
    return props;
}

void merge_properties(gr::property_map& base, const gr::property_map& overrides) {
    for (const auto& [key, val] : overrides) {
        base.insert_or_assign(std::pmr::string(std::string(key)), val);
    }
}

// --- CLI ---

void print_usage() {
    std::fprintf(stderr, "Usage: lora_trx [options]\n\n"
                         "Full-duplex LoRa transceiver with CBOR-over-UDP I/O.\n\n"
                         "Options:\n"
                         "  --config <file>       TOML configuration file (default: config.toml)\n"
                         "  --log-level <level>   Log level: DEBUG, INFO, WARNING, ERROR\n"
                         "                        Overrides [logging] level in config.toml.\n"
                         "                        DEBUG also enables verbose decoder traces.\n"
                         "  --version             Show version and exit\n"
                         "  -h, --help            Show this help\n");
}

int parse_args(int argc, char* argv[], std::string& config_path, std::string& log_level) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            print_usage();
            return 2;
        }
        if (arg == "--version") {
            std::fprintf(stderr, "lora_trx %s\n", GIT_REV);
            return 2;
        }
        if (arg == "--config" && i + 1 < argc) {
            config_path = argv[++i];
            continue;
        }
        if (arg == "--log-level" && i + 1 < argc) {
            log_level = argv[++i];
            // Uppercase for consistency
            for (auto& ch : log_level) {
                ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
            }
            continue;
        }
        std::fprintf(stderr, "Unknown argument: %s\n", arg.c_str());
        print_usage();
        return 1;
    }
    if (config_path.empty()) {
        config_path = "config.toml";
    }
    return 0;
}

// --- Shared TOML helpers ---

struct ParsedDevice {
    std::string device{};
    std::string param{};
    std::string clock{};
};

static ParsedDevice parse_device(const toml::table& tbl) {
    ParsedDevice d;
    auto*        dev_tbl = tbl["device"].as_table();
    if (!dev_tbl) {
        throw std::runtime_error("[device] section is required in config (set driver and param)");
    }
    auto dev_or_s = [&](std::string_view key, std::string def) -> std::string {
        if (auto v = dev_tbl->at_path(key).value<std::string>()) {
            return *v;
        }
        return def;
    };
    d.device = dev_or_s("driver", dev_or_s("device", ""));
    d.param  = dev_or_s("param", "");
    if (d.device.empty()) {
        throw std::runtime_error("[device].driver is required (e.g. \"uhd\", \"lime\", \"loopback\")");
    }
    d.clock = dev_or_s("clock", "");
    return d;
}

static void parse_logging(const toml::table& tbl, const std::string& cli_log_level) {
    auto*       log_tbl = tbl["logging"].as_table();
    std::string level   = "INFO";
    if (log_tbl) {
        level = log_tbl->at_path("level").value_or(std::string{"INFO"});
    }
    if (!cli_log_level.empty()) {
        level = cli_log_level;
    }
    gr::lora::set_log_level(level);
}

struct RadioCfg {
    double                   freq{869'618'000.0};
    double                   rx_gain{30.0};
    double                   tx_gain{75.0};
    std::vector<uint32_t>    rx_channels{0}; ///< SoapySDR RX channel indices
    uint32_t                 tx_channel{0};  ///< SoapySDR TX channel index
    std::vector<std::string> rx_antenna{};   ///< per-channel RX antenna (empty = driver default)
    std::string              tx_antenna{};   ///< TX antenna (empty = driver default)
    double                   lo_offset{0.0};
    bool                     dc_offset_auto{true};
};

static std::map<std::string, RadioCfg> parse_radios(const toml::table& tbl) {
    std::map<std::string, RadioCfg> radios;
    for (auto&& [key, val] : tbl) {
        if (!val.is_table()) {
            continue;
        }
        std::string skey(key.str());
        if (!skey.starts_with("radio_")) {
            continue;
        }
        auto&    section = *val.as_table();
        RadioCfg r;
        r.freq    = section["freq"].value_or(869'618'000.0);
        r.rx_gain = section["rx_gain"].value_or(35.0);
        r.tx_gain = section["tx_gain"].value_or(70.0);
        if (auto* arr = section["rx_channel"].as_array()) {
            r.rx_channels.clear();
            for (auto& elem : *arr) {
                r.rx_channels.push_back(static_cast<uint32_t>(elem.value_or(int64_t{0})));
            }
        }
        r.tx_channel = static_cast<uint32_t>(section["tx_channel"].value_or(int64_t{0}));
        if (auto* arr = section["rx_antenna"].as_array()) {
            for (auto& elem : *arr) {
                if (auto v = elem.value<std::string>()) {
                    r.rx_antenna.push_back(*v);
                }
            }
        }
        r.tx_antenna     = section["tx_antenna"].value_or(std::string{});
        r.lo_offset      = section["lo_offset"].value_or(0.0);
        r.dc_offset_auto = section["dc_offset_auto"].value_or(true);
        radios[skey]     = r;
    }
    return radios;
}

// --- TOML config loader ---

std::vector<TrxConfig> load_config(const std::string& path, const std::string& cli_log_level) {
    toml::table tbl;
    try {
        tbl = toml::parse_file(path);
    } catch (const toml::parse_error& err) {
        gr::lora::log_ts("error", "config", "%s: %s", path.c_str(), err.description().data());
        return {};
    }

    auto dev = parse_device(tbl);
    parse_logging(tbl, cli_log_level);
    auto        radios          = parse_radios(tbl);
    std::string effective_level = cli_log_level;
    if (effective_level.empty()) {
        auto* log_tbl = tbl["logging"].as_table();
        if (log_tbl) {
            effective_level = log_tbl->at_path("level").value_or(std::string{"INFO"});
        }
    }
    bool g_debug = (effective_level == "DEBUG");

    // --- [trx] section ---
    auto* trx_tbl = tbl["trx"].as_table();
    if (!trx_tbl) {
        gr::lora::log_ts("error", "config", "[trx] section not found");
        return {};
    }

    // Reject the old [codec_*]-reference schema with a migration hint.
    if (trx_tbl->at_path("codec").node() || trx_tbl->at_path("decode_bws").node() || trx_tbl->at_path("decode").node()) {
        gr::lora::log_ts("error", "config",
            "[trx]: old 'codec'/'decode_bws'/[[trx.decode]] schema removed; "
            "use [trx.transmit], [trx.receive].bandwidths, [[trx.receive.chain]]");
        return {};
    }

    auto radio_ref = trx_tbl->at_path("radio").value<std::string>();
    if (!radio_ref) {
        gr::lora::log_ts("error", "config", "[trx] requires 'radio' key");
        return {};
    }

    auto rit = radios.find(*radio_ref);
    if (rit == radios.end()) {
        gr::lora::log_ts("error", "config", "[trx] references unknown radio '%s'", radio_ref->c_str());
        return {};
    }

    const auto& radio = rit->second;

    TrxConfig cfg;
    cfg.device       = dev.device;
    cfg.device_param = dev.param;
    cfg.clock        = dev.clock;
    cfg.debug        = g_debug;

    cfg.name = trx_tbl->at_path("name").value_or(std::string{"trx"});
    cfg.rate = static_cast<float>(trx_tbl->at_path("rate").value_or(250'000.0));

    // Radio
    cfg.freq           = radio.freq;
    cfg.gain_rx        = radio.rx_gain;
    cfg.gain_tx        = radio.tx_gain;
    cfg.rx_channels    = radio.rx_channels;
    cfg.tx_channel     = radio.tx_channel;
    cfg.rx_antenna     = radio.rx_antenna;
    cfg.tx_antenna     = radio.tx_antenna;
    cfg.lo_offset      = radio.lo_offset;
    cfg.dc_offset_auto = radio.dc_offset_auto;

    // [trx.transmit] — TX modulation preset (required)
    auto* tx_tbl = trx_tbl->at_path("transmit").as_table();
    if (!tx_tbl) {
        gr::lora::log_ts("error", "config", "[trx.transmit] section is required (TX modulation preset)");
        return {};
    }
    cfg.sf       = static_cast<uint8_t>(tx_tbl->at_path("sf").value_or(int64_t{8}));
    cfg.bw       = static_cast<uint32_t>(tx_tbl->at_path("bw").value_or(int64_t{62'500}));
    cfg.sync     = static_cast<uint16_t>(tx_tbl->at_path("sync_word").value_or(int64_t{0x12}));
    cfg.preamble = static_cast<uint16_t>(tx_tbl->at_path("preamble_len").value_or(int64_t{16}));
    auto cr_val  = static_cast<uint8_t>(tx_tbl->at_path("cr").value_or(int64_t{8}));
    if (cr_val < 5 || cr_val > 8) {
        gr::lora::log_ts("error", "config", "[trx.transmit].cr must be 5-8 (denominator), got %u", cr_val);
        return {};
    }
    cfg.cr = static_cast<uint8_t>(cr_val - 4);
    if (cfg.sf < 7 || cfg.sf > 12) {
        gr::lora::log_ts("error", "config", "[trx.transmit].sf must be 7-12, got %u", cfg.sf);
        return {};
    }

    // [trx.network]
    auto* net_tbl = trx_tbl->at_path("network").as_table();
    if (net_tbl) {
        cfg.listen          = net_tbl->at_path("udp_listen").value_or(std::string{"127.0.0.1"});
        cfg.port            = static_cast<uint16_t>(net_tbl->at_path("udp_port").value_or(int64_t{5556}));
        cfg.status_interval = static_cast<uint32_t>(net_tbl->at_path("status_interval").value_or(int64_t{10}));
        cfg.lbt             = net_tbl->at_path("lbt").value_or(true);
        cfg.lbt_timeout_ms  = static_cast<uint32_t>(net_tbl->at_path("lbt_timeout_ms").value_or(int64_t{2000}));
        cfg.tx_queue_depth  = static_cast<uint32_t>(net_tbl->at_path("tx_queue_depth").value_or(int64_t{8}));
    }

    // Raw config passthrough for Python scripts
    cfg.raw.network.host            = cfg.listen;
    cfg.raw.network.port            = cfg.port;
    cfg.raw.network.status_interval = cfg.status_interval;
    cfg.raw.network.lbt             = cfg.lbt;
    cfg.raw.network.lbt_timeout_ms  = cfg.lbt_timeout_ms;
    cfg.raw.network.tx_queue_depth  = cfg.tx_queue_depth;

    auto* agg_tbl = tbl["aggregator"].as_table();
    if (agg_tbl) {
        cfg.raw.aggregator.upstream  = agg_tbl->at_path("upstream").value_or(std::string{"127.0.0.1:5556"});
        cfg.raw.aggregator.listen    = agg_tbl->at_path("listen").value_or(std::string{"127.0.0.1:5555"});
        cfg.raw.aggregator.window_ms = static_cast<uint32_t>(agg_tbl->at_path("window_ms").value_or(int64_t{200}));
    }

    auto* mc_tbl = tbl["meshcore"].as_table();
    if (mc_tbl) {
        auto& mc            = cfg.raw.meshcore;
        mc.name             = mc_tbl->at_path("name").value_or(std::string{});
        mc.model            = mc_tbl->at_path("model").value_or(std::string{});
        mc.identity_file    = mc_tbl->at_path("identity_file").value_or(std::string{});
        mc.keys_dir         = mc_tbl->at_path("keys_dir").value_or(std::string{});
        mc.channels_dir     = mc_tbl->at_path("channels_dir").value_or(std::string{});
        mc.contacts_dir     = mc_tbl->at_path("contacts_dir").value_or(std::string{});
        mc.lat              = mc_tbl->at_path("lat").value_or(0.0);
        mc.lon              = mc_tbl->at_path("lon").value_or(0.0);
        mc.port             = static_cast<uint16_t>(mc_tbl->at_path("port").value_or(int64_t{7834}));
        mc.region_scope     = mc_tbl->at_path("region_scope").value_or(std::string{});
        mc.flood_scope      = mc_tbl->at_path("flood_scope").value_or(std::string{});
        mc.client_repeat    = static_cast<uint32_t>(mc_tbl->at_path("client_repeat").value_or(int64_t{0}));
        mc.path_hash_mode   = static_cast<uint32_t>(mc_tbl->at_path("path_hash_mode").value_or(int64_t{0}));
        mc.autoadd_config   = static_cast<uint32_t>(mc_tbl->at_path("autoadd_config").value_or(int64_t{0}));
        mc.autoadd_max_hops = static_cast<uint32_t>(mc_tbl->at_path("autoadd_max_hops").value_or(int64_t{0}));
    }

    auto* log_tbl = tbl["logging"].as_table();
    if (log_tbl) {
        cfg.raw.logging.level = log_tbl->at_path("level").value_or(std::string{"INFO"});
        cfg.raw.logging.color = log_tbl->at_path("color").value_or(true);
    }
    if (!cli_log_level.empty()) {
        cfg.raw.logging.level = cli_log_level;
    }

    // [trx.receive] — RX pipeline configuration (required)
    auto* rx_tbl = trx_tbl->at_path("receive").as_table();
    if (!rx_tbl) {
        gr::lora::log_ts("error", "config", "[trx.receive] section is required (bandwidths + [[trx.receive.chain]])");
        return {};
    }

    // [trx.receive].bandwidths — required non-empty list
    if (auto* bw_arr = rx_tbl->at_path("bandwidths").as_array()) {
        for (auto& elem : *bw_arr) {
            if (auto v = elem.value<int64_t>()) {
                cfg.rx_bandwidths.push_back(static_cast<uint32_t>(*v));
            }
        }
    }
    if (cfg.rx_bandwidths.empty()) {
        gr::lora::log_ts("error", "config", "[trx.receive].bandwidths must be a non-empty list (e.g. [62500, 125000, 250000])");
        return {};
    }

    // [[trx.receive.chain]] entries
    static constexpr std::array kChainIdentityKeys = {
        std::string_view{"sf"},
        std::string_view{"sync_word"},
        std::string_view{"label"},
    };

    if (auto* chain_arr = rx_tbl->at_path("chain").as_array()) {
        for (auto& elem : *chain_arr) {
            if (!elem.is_table()) {
                continue;
            }
            auto&        dtbl = *elem.as_table();
            DecodeConfig dc;

            // sync_word: optional. Unset = promiscuous (decode any sync word,
            // surface observed value per-frame).
            if (auto sw_opt = dtbl["sync_word"].value<int64_t>()) {
                if (*sw_opt < 0 || *sw_opt > 0xFFFF) {
                    gr::lora::log_ts("error", "config", "[[trx.receive.chain]] sync_word must be 0..0xFFFF, got %" PRId64, *sw_opt);
                    return {};
                }
                dc.sync_word = static_cast<uint16_t>(*sw_opt);
            }

            auto lbl_opt = dtbl["label"].value<std::string>();
            if (!lbl_opt || lbl_opt->empty()) {
                gr::lora::log_ts("error", "config", "[[trx.receive.chain]] entry missing 'label'");
                return {};
            }
            dc.label = *lbl_opt;

            // sf: optional list. Empty/unset = sweep SF7-12. Each entry ∈ [7..12].
            if (auto* sf_arr = dtbl["sf"].as_array()) {
                for (auto& sf_elem : *sf_arr) {
                    auto sf_val = sf_elem.value<int64_t>();
                    if (!sf_val) {
                        gr::lora::log_ts("error", "config", "[[trx.receive.chain]] sf list must contain integers");
                        return {};
                    }
                    if (*sf_val < 7 || *sf_val > 12) {
                        gr::lora::log_ts("error", "config", "[[trx.receive.chain]] sf entry %" PRId64 " out of range [7..12]", *sf_val);
                        return {};
                    }
                    auto v = static_cast<uint8_t>(*sf_val);
                    if (std::ranges::find(dc.sf_set, v) != dc.sf_set.end()) {
                        gr::lora::log_ts("error", "config", "[[trx.receive.chain]] sf list has duplicate entry SF%u", v);
                        return {};
                    }
                    dc.sf_set.push_back(v);
                }
            }

            for (const auto& [okey, oval] : dtbl) {
                std::string ok_str(okey.str());
                if (std::ranges::any_of(kChainIdentityKeys, [&](auto k) { return k == ok_str; })) {
                    continue;
                }
                if (oval.is_table()) {
                    continue;
                }
                if (oval.is_array()) {
                    continue; // sf lists handled above
                }
                toml::table tmp;
                tmp.insert(okey, oval);
                auto single = toml_to_property_map(tmp);
                for (auto& [pk, pv] : single) {
                    dc.block_overrides.insert_or_assign(pk, pv);
                }
            }

            cfg.rx_chains.push_back(std::move(dc));
        }
    }
    if (cfg.rx_chains.empty()) {
        gr::lora::log_ts("error", "config", "[trx.receive] requires at least one [[trx.receive.chain]] entry");
        return {};
    }

    // Chain label uniqueness (required for downstream routing via `decode_label`).
    // sync_word and sf_set uniqueness are NOT enforced — a user may intentionally
    // run two promiscuous chains with different min_snr_db gates, or pin a chain
    // to SF8 while another sweeps.
    for (std::size_t i = 0; i < cfg.rx_chains.size(); ++i) {
        for (std::size_t j = i + 1; j < cfg.rx_chains.size(); ++j) {
            if (cfg.rx_chains[i].label == cfg.rx_chains[j].label) {
                gr::lora::log_ts("error", "config", "[[trx.receive.chain]] duplicate label '%s' at entries %zu and %zu", cfg.rx_chains[i].label.c_str(), i, j);
                return {};
            }
        }
    }

    // rx_bandwidths validation: each entry must be positive.  Duplicates are
    // permitted for symmetry with chain-level permissiveness (the user is
    // allowed to spawn redundant decoders if they really want).
    for (std::size_t i = 0; i < cfg.rx_bandwidths.size(); ++i) {
        if (cfg.rx_bandwidths[i] == 0) {
            gr::lora::log_ts("error", "config", "[trx.receive].bandwidths[%zu] = 0 — must be positive (Hz)", i);
            return {};
        }
    }

    // soft decode (experimental)
    if (auto v = trx_tbl->at_path("soft_decode").value<bool>()) {
        cfg.soft_decode = *v;
    }

    // half-band FIR anti-alias in narrowband decimation path
    if (auto v = trx_tbl->at_path("use_aa_filter").value<bool>()) {
        cfg.use_aa_filter = *v;
    }

    // enable_tx: when false, skip TX graph construction entirely. Useful
    // as a workaround for the SoapySink dual-channel TX STREAM_ERROR on
    // B210/B220 via UHD, and for any RX-only monitor deployment.
    if (auto v = trx_tbl->at_path("enable_tx").value<bool>()) {
        cfg.enable_tx = *v;
    }

    // Validate: rx_channel values are SoapySDR channel indices (0-based)
    if (cfg.rx_channels.empty()) {
        gr::lora::log_ts("error", "config", "rx_channel must have at least one entry");
        return {};
    }
    if (!cfg.rx_antenna.empty() && cfg.rx_antenna.size() != cfg.rx_channels.size()) {
        gr::lora::log_ts("error", "config", "rx_antenna has %zu entries but rx_channel has %zu — must match or be empty", cfg.rx_antenna.size(), cfg.rx_channels.size());
        return {};
    }

    // (Startup banner is emitted by lora_trx/lora_scan after config load;
    // no separate summary line here to avoid duplication.)

    return {std::move(cfg)};
}

// --- Scan config loader ---

std::vector<ScanSetConfig> load_scan_config(const std::string& path, const std::string& cli_log_level) {
    toml::table tbl;
    try {
        tbl = toml::parse_file(path);
    } catch (const toml::parse_error& err) {
        gr::lora::log_ts("error", "config", "%s: %s", path.c_str(), err.description().data());
        return {};
    }

    auto dev = parse_device(tbl);
    parse_logging(tbl, cli_log_level);
    auto radios = parse_radios(tbl);

    // --- [scan] section ---
    auto* scan_tbl = tbl["scan"].as_table();
    if (!scan_tbl) {
        gr::lora::log_ts("error", "config", "[scan] section not found");
        return {};
    }

    // Radio reference (optional)
    RadioCfg radio;
    if (auto radio_ref = scan_tbl->at_path("radio").value<std::string>()) {
        auto rit = radios.find(*radio_ref);
        if (rit == radios.end()) {
            gr::lora::log_ts("error", "config", "[scan] references unknown radio '%s'", radio_ref->c_str());
            return {};
        }
        radio = rit->second;
    }

    ScanSetConfig cfg;
    cfg.device         = dev.device;
    cfg.device_param   = dev.param;
    cfg.clock          = dev.clock;
    cfg.l1_rate        = scan_tbl->at_path("l1_rate").value_or(16.0e6);
    cfg.master_clock   = scan_tbl->at_path("master_clock").value_or(24.0e6);
    cfg.gain           = scan_tbl->at_path("rx_gain").value_or(radio.rx_gain);
    cfg.freq_start     = scan_tbl->at_path("freq_start").value_or(radio.freq);
    cfg.freq_stop      = scan_tbl->at_path("freq_stop").value_or(cfg.freq_start);
    cfg.lo_offset      = radio.lo_offset;
    cfg.dc_offset_auto = radio.dc_offset_auto;

    if (cfg.freq_start >= cfg.freq_stop) {
        gr::lora::log_ts("error", "config", "[scan] freq_start (%.0f) must be < freq_stop (%.0f)", cfg.freq_start, cfg.freq_stop);
        return {};
    }

    cfg.os_factor   = static_cast<uint32_t>(scan_tbl->at_path("os_factor").value_or(int64_t{4}));
    cfg.min_ratio   = static_cast<float>(scan_tbl->at_path("min_ratio").value_or(5.0));
    cfg.settle_ms   = static_cast<int>(scan_tbl->at_path("settle_ms").value_or(int64_t{5}));
    cfg.sweeps      = static_cast<uint32_t>(scan_tbl->at_path("sweeps").value_or(int64_t{0}));
    cfg.layer1_only = scan_tbl->at_path("layer1_only").value_or(false);
    cfg.streaming   = scan_tbl->at_path("streaming").value_or(true);

    // Streaming-mode parameters
    cfg.buffer_ms   = static_cast<float>(scan_tbl->at_path("buffer_ms").value_or(512.0));
    cfg.l1_fft_size = static_cast<uint32_t>(scan_tbl->at_path("l1_fft_size").value_or(int64_t{4096}));
    cfg.channel_bw  = static_cast<float>(scan_tbl->at_path("channel_bw").value_or(62500.0));

    // [scan.network]
    if (auto* net_tbl = scan_tbl->at_path("network").as_table()) {
        cfg.udp_listen = net_tbl->at_path("udp_listen").value_or(std::string{"127.0.0.1"});
        cfg.udp_port   = static_cast<uint16_t>(net_tbl->at_path("udp_port").value_or(int64_t{5557}));
    }

    // BWs
    cfg.bws.clear();
    if (auto* bw_arr = scan_tbl->at_path("bws").as_array()) {
        for (auto& elem : *bw_arr) {
            if (auto v = elem.value<int64_t>()) {
                cfg.bws.push_back(static_cast<uint32_t>(*v));
            }
        }
    }
    if (cfg.bws.empty()) {
        cfg.bws = {62500, 125000, 250000};
    }
    std::ranges::sort(cfg.bws);

    // Validate decimation factors
    for (uint32_t bw : cfg.bws) {
        const double targetRate = static_cast<double>(bw) * static_cast<double>(cfg.os_factor);
        const double factor     = cfg.l1_rate / targetRate;
        const double rounded    = std::round(factor);
        if (std::abs(factor - rounded) > 0.01 || rounded < 1.0) {
            gr::lora::log_ts("error", "config", "[scan] l1_rate %.0f / (BW %u x os %u) = %.3f -- must be integer", cfg.l1_rate, bw, cfg.os_factor, factor);
            return {};
        }
    }

    {
        std::string bw_list;
        for (std::size_t i = 0; i < cfg.bws.size(); i++) {
            if (i > 0) {
                bw_list += ",";
            }
            if (cfg.bws[i] >= 1000) {
                bw_list += std::to_string(cfg.bws[i] / 1000) + "k";
            } else {
                bw_list += std::to_string(cfg.bws[i]);
            }
        }
        gr::lora::log_ts("info ", "config", "scan: %.3f-%.3f MHz  BW=[%s]  os=%u  L1=%.1f MS/s", cfg.freq_start / 1e6, cfg.freq_stop / 1e6, bw_list.c_str(), cfg.os_factor, cfg.l1_rate / 1e6);
    }

    return {std::move(cfg)};
}

// --- CBOR serialisation ---

// Helper: count non-empty string fields to determine map size for optional keys.
static uint32_t count_meshcore_fields(const RawConfig::MeshCore& mc) {
    uint32_t n = 0;
    if (!mc.name.empty()) {
        n++;
    }
    if (!mc.model.empty()) {
        n++;
    }
    if (!mc.identity_file.empty()) {
        n++;
    }
    if (!mc.keys_dir.empty()) {
        n++;
    }
    if (!mc.channels_dir.empty()) {
        n++;
    }
    if (!mc.contacts_dir.empty()) {
        n++;
    }
    if (mc.lat != 0.0 || mc.lon != 0.0) {
        n += 2; // lat + lon
    }
    n++; // port (always present)
    if (!mc.region_scope.empty()) {
        n++;
    }
    if (!mc.flood_scope.empty()) {
        n++;
    }
    n++; // client_repeat
    n++; // path_hash_mode
    n++; // autoadd_config
    n++; // autoadd_max_hops
    return n;
}

std::vector<uint8_t> build_config_cbor(const TrxConfig& cfg, const std::string& device_serial) {
    namespace cbor = gr::lora::cbor;
    std::vector<uint8_t> buf;
    buf.reserve(512);

    cbor::encode_map_begin(buf, 5); // type + phy + server + rx_chains + raw
    cbor::kv_text(buf, "type", "config");

    cbor::encode_text(buf, "phy");
    cbor::encode_map_begin(buf, 8);
    cbor::kv_float64(buf, "freq", cfg.freq);
    cbor::kv_uint(buf, "sf", cfg.sf);
    cbor::kv_uint(buf, "bw", cfg.bw);
    cbor::kv_uint(buf, "cr", cfg.cr);
    cbor::kv_uint(buf, "sync_word", cfg.sync);
    cbor::kv_uint(buf, "preamble", cfg.preamble);
    cbor::kv_float64(buf, "rx_gain", cfg.gain_rx);
    cbor::kv_float64(buf, "tx_gain", cfg.gain_tx);

    cbor::encode_text(buf, "server");
    uint32_t server_fields = 4; // device + status_interval + sample_rate + tx_enabled
    if (!device_serial.empty()) {
        server_fields++;
    }
    cbor::encode_map_begin(buf, server_fields);
    cbor::kv_text(buf, "device", cfg.device);
    cbor::kv_uint(buf, "status_interval", cfg.status_interval);
    cbor::kv_float64(buf, "sample_rate", static_cast<double>(cfg.rate));
    // tx_enabled tells clients whether lora_tx requests will be honoured.
    cbor::kv_bool(buf, "tx_enabled", cfg.enable_tx);
    if (!device_serial.empty()) {
        cbor::kv_text(buf, "device_serial", device_serial);
    }

    // RX chain summary.  sync_word absent = promiscuous (decoder surfaces
    // observed value per-frame).  sf list empty = sweep SF7-12.
    cbor::encode_text(buf, "rx_chains");
    cbor::encode_array_begin(buf, static_cast<uint32_t>(cfg.rx_chains.size()));
    for (const auto& dc : cfg.rx_chains) {
        uint32_t n_fields = 3; // label + promiscuous + sf (may be empty array)
        if (dc.sync_word.has_value()) {
            n_fields++;
        }
        cbor::encode_map_begin(buf, n_fields);
        cbor::kv_text(buf, "label", dc.label);
        cbor::kv_bool(buf, "promiscuous", !dc.sync_word.has_value());
        if (dc.sync_word.has_value()) {
            cbor::kv_uint(buf, "sync_word", *dc.sync_word);
        }
        cbor::encode_text(buf, "sf");
        cbor::encode_array_begin(buf, static_cast<uint32_t>(dc.sf_set.size()));
        for (uint8_t s : dc.sf_set) {
            cbor::encode_uint(buf, s);
        }
    }

    // Raw passthrough sections for Python scripts
    const auto& raw = cfg.raw;
    cbor::encode_text(buf, "raw");
    cbor::encode_map_begin(buf, 4); // network + aggregator + meshcore + logging

    // raw.network
    cbor::encode_text(buf, "network");
    cbor::encode_map_begin(buf, 6);
    cbor::kv_text(buf, "udp_listen", raw.network.host);
    cbor::kv_uint(buf, "udp_port", raw.network.port);
    cbor::kv_uint(buf, "status_interval", raw.network.status_interval);
    cbor::kv_bool(buf, "lbt", raw.network.lbt);
    cbor::kv_uint(buf, "lbt_timeout_ms", raw.network.lbt_timeout_ms);
    cbor::kv_uint(buf, "tx_queue_depth", raw.network.tx_queue_depth);

    // raw.aggregator
    cbor::encode_text(buf, "aggregator");
    cbor::encode_map_begin(buf, 3);
    cbor::kv_text(buf, "upstream", raw.aggregator.upstream);
    cbor::kv_text(buf, "listen", raw.aggregator.listen);
    cbor::kv_uint(buf, "window_ms", raw.aggregator.window_ms);

    // raw.meshcore
    cbor::encode_text(buf, "meshcore");
    auto mc_fields = count_meshcore_fields(raw.meshcore);
    cbor::encode_map_begin(buf, mc_fields);
    if (!raw.meshcore.name.empty()) {
        cbor::kv_text(buf, "name", raw.meshcore.name);
    }
    if (!raw.meshcore.model.empty()) {
        cbor::kv_text(buf, "model", raw.meshcore.model);
    }
    if (!raw.meshcore.identity_file.empty()) {
        cbor::kv_text(buf, "identity_file", raw.meshcore.identity_file);
    }
    if (!raw.meshcore.keys_dir.empty()) {
        cbor::kv_text(buf, "keys_dir", raw.meshcore.keys_dir);
    }
    if (!raw.meshcore.channels_dir.empty()) {
        cbor::kv_text(buf, "channels_dir", raw.meshcore.channels_dir);
    }
    if (!raw.meshcore.contacts_dir.empty()) {
        cbor::kv_text(buf, "contacts_dir", raw.meshcore.contacts_dir);
    }
    if (raw.meshcore.lat != 0.0 || raw.meshcore.lon != 0.0) {
        cbor::kv_float64(buf, "lat", raw.meshcore.lat);
        cbor::kv_float64(buf, "lon", raw.meshcore.lon);
    }
    cbor::kv_uint(buf, "port", raw.meshcore.port);
    if (!raw.meshcore.region_scope.empty()) {
        cbor::kv_text(buf, "region_scope", raw.meshcore.region_scope);
    }
    if (!raw.meshcore.flood_scope.empty()) {
        cbor::kv_text(buf, "flood_scope", raw.meshcore.flood_scope);
    }
    cbor::kv_uint(buf, "client_repeat", raw.meshcore.client_repeat);
    cbor::kv_uint(buf, "path_hash_mode", raw.meshcore.path_hash_mode);
    cbor::kv_uint(buf, "autoadd_config", raw.meshcore.autoadd_config);
    cbor::kv_uint(buf, "autoadd_max_hops", raw.meshcore.autoadd_max_hops);

    // raw.logging
    cbor::encode_text(buf, "logging");
    cbor::encode_map_begin(buf, 2);
    cbor::kv_text(buf, "level", raw.logging.level);
    cbor::kv_bool(buf, "color", raw.logging.color);

    return buf;
}

std::vector<uint8_t> build_status_cbor(const TrxConfig& cfg, SharedStatus& status, const char* git_rev, const std::string& device_serial) {
    namespace cbor = gr::lora::cbor;
    std::vector<uint8_t> buf;
    buf.reserve(128);

    uint32_t total    = status.frame_count.load();
    uint32_t ok_cnt   = status.crc_ok.load();
    uint32_t fail_cnt = status.crc_fail.load();
    uint64_t ovf_cnt  = status.overflow_count.load();

    uint32_t n_fields   = 5; // type, ts, phy, frames, rx_overflows
    bool     has_rev    = (git_rev != nullptr && git_rev[0] != '\0');
    bool     has_serial = !device_serial.empty();
    if (has_rev) {
        n_fields++;
    }
    if (has_serial) {
        n_fields++;
    }
    cbor::encode_map_begin(buf, n_fields);
    cbor::kv_text(buf, "type", "status");

    auto now = std::chrono::system_clock::now();
    auto tt  = std::chrono::system_clock::to_time_t(now);
    char ts[32]{};
    std::strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&tt));
    cbor::kv_text(buf, "ts", ts);

    if (has_rev) {
        cbor::kv_text(buf, "git_rev", git_rev);
    }
    if (has_serial) {
        cbor::kv_text(buf, "device", device_serial);
    }

    cbor::encode_text(buf, "phy");
    cbor::encode_map_begin(buf, 2);
    cbor::kv_float64(buf, "rx_gain", cfg.gain_rx);
    cbor::kv_float64(buf, "tx_gain", cfg.gain_tx);

    cbor::encode_text(buf, "frames");
    cbor::encode_map_begin(buf, 3);
    cbor::kv_uint(buf, "total", total);
    cbor::kv_uint(buf, "crc_ok", ok_cnt);
    cbor::kv_uint(buf, "crc_fail", fail_cnt);

    cbor::kv_uint(buf, "rx_overflows", ovf_cnt);

    return buf;
}

std::vector<uint8_t> build_spectrum_cbor(gr::lora::SpectrumState& spec, const char* type_name) {
    namespace cbor = gr::lora::cbor;
    std::vector<uint8_t> buf;
    buf.reserve(64 + spec.fft_size * sizeof(float));
    cbor::encode_map_begin(buf, 5);
    cbor::kv_text(buf, "type", type_name);
    {
        std::lock_guard lock(spec.result_mutex);
        cbor::kv_bytes(buf, "bins", reinterpret_cast<const uint8_t*>(spec.magnitude_db.data()), spec.magnitude_db.size() * sizeof(float));
        spec.result_ready.store(false, std::memory_order_relaxed);
    }
    cbor::kv_uint(buf, "fft_size", spec.fft_size);
    cbor::kv_float64(buf, "center_freq", spec.center_freq);
    cbor::kv_float64(buf, "sample_rate", static_cast<double>(spec.sample_rate));
    return buf;
}

} // namespace lora_config
