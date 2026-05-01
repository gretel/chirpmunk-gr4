// SPDX-License-Identifier: ISC
//
// Configuration structs, TOML parser, and CBOR serialisation for lora apps.
//
// The `RawConfig` struct carries passthrough TOML sections (network,
// aggregator, meshcore, logging) for CBOR distribution to Python scripts.

#ifndef GR4_LORA_CONFIG_HPP
#define GR4_LORA_CONFIG_HPP

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include <gnuradio-4.0/Block.hpp> // property_map
#include <gnuradio-4.0/lora/algorithm/SpectrumTap.hpp>
#include <gnuradio-4.0/lora/cbor.hpp>

namespace lora_config {

// Per-RX-chain configuration.  Each entry produces one MultiSfDecoder
// per BW per radio channel.  Multiple chain entries on the same radio
// channel are split via a Splitter block.
//
// sync_word semantics:
//   - std::nullopt : promiscuous — decoder locks on any sync word and
//                    surfaces the observed value per frame (LoRaWAN
//                    0x34, MeshCore 0x12, private nets, etc. all caught).
//   - value set    : strict filter — only frames with this sync word
//                    advance past PreambleSync stage 4.
//
// sf_set semantics:
//   - empty        : sweep SF7-12 (default, same CPU cost as before).
//   - [N]          : pin single SF (lower CPU / latency; only decodes N).
//   - [N1, N2, …]  : explicit non-contiguous subset (e.g. {8, 11}).
//
// `label` is required and must be unique across chains.
struct DecodeConfig {
    std::optional<uint16_t> sync_word{}; ///< unset = promiscuous
    std::vector<uint8_t>    sf_set{};    ///< empty = sweep SF7-12
    std::string             label{};
    gr::property_map        block_overrides{}; ///< per-chain MultiSfDecoder overrides
};

// Passthrough config sections for Python scripts.
// Mirrors the TOML section structure so Python gets the same nested dict
// it would have gotten from parsing config.toml directly.
struct RawConfig {
    // [network] — UDP addressing (scripts resolve listen addresses from here)
    struct Network {
        std::string host{"127.0.0.1"};
        uint16_t    port{5556};
        uint32_t    status_interval{10};
        bool        lbt{true};
        uint32_t    lbt_timeout_ms{2000};
        uint32_t    tx_queue_depth{4};
    } network;

    // [aggregator] — lora_agg.py settings
    struct Aggregator {
        std::string upstream{"127.0.0.1:5556"};
        std::string listen{"127.0.0.1:5555"};
        uint32_t    window_ms{200};
    } aggregator;

    // [meshcore] — identity, location, flood scope (Python meshcore_bridge.py)
    struct MeshCore {
        std::string name{};
        std::string model{};
        std::string identity_file{};
        std::string keys_dir{};
        std::string channels_dir{};
        std::string contacts_dir{};
        double      lat{0.0};
        double      lon{0.0};
        uint16_t    port{7834};
        std::string region_scope{};
        std::string flood_scope{};
        uint32_t    client_repeat{0};
        uint32_t    path_hash_mode{0};
        uint32_t    autoadd_config{0};
        uint32_t    autoadd_max_hops{0};
    } meshcore;

    // [logging] — Python log level and color
    struct Logging {
        std::string level{"INFO"};
        bool        color{true};
    } logging;
};

struct TrxConfig {
    std::string name{};
    std::string device{};
    std::string device_param{};
    double      freq{869'618'000.0};
    double      gain_rx{35.0};
    double      gain_tx{70.0};
    float       rate{250'000.f};
    // TX modulation preset ([trx.transmit]).  Used only when transmitting.
    // RX side is decoupled: bandwidths from rx_bandwidths, SF subset from sf_set per chain.
    // `preamble` is also used as the RX preamble-detection threshold (physical
    // agreement — transmitter and receiver must share this parameter).
    uint32_t                 bw{62'500};   ///< TX BW (Hz)
    uint8_t                  sf{8};        ///< TX SF (7..12)
    uint8_t                  cr{4};        ///< TX coding-rate denominator - 4 (0..4)
    uint16_t                 sync{0x12};   ///< TX sync word
    uint16_t                 preamble{16}; ///< TX preamble length (MeshCore RadioLib default); also RX detection threshold
    std::string              clock{};
    std::vector<uint32_t>    rx_channels{0}; ///< SoapySDR RX channel indices
    uint32_t                 tx_channel{0};  ///< SoapySDR TX channel index
    std::vector<std::string> rx_antenna{};   ///< per-channel RX antenna (empty = driver default)
    std::string              tx_antenna{};   ///< TX antenna (empty = driver default)
    std::string              listen{"127.0.0.1"};
    uint16_t                 port{5556};
    uint32_t                 status_interval{10}; ///< seconds between status heartbeats (0 = off)
    bool                     debug{false};
    bool                     enable_tx{true};      ///< build + run TX graph (false = RX-only mode)
    bool                     lbt{true};            ///< enable LBT (CAD-based)
    uint32_t                 lbt_timeout_ms{2000}; ///< max wait for channel clear before rejecting TX
    uint32_t                 tx_queue_depth{8};    ///< max queued TX requests (rejects when full)

    std::vector<DecodeConfig> rx_chains{};     ///< [[trx.receive.chain]] entries (at least one required)
    std::vector<uint32_t>     rx_bandwidths{}; ///< [trx.receive].bandwidths — must be non-empty
    RawConfig                 raw{};           ///< passthrough sections for Python scripts

    // DC spur mitigation (from [radio_*])
    double lo_offset{0.0};       ///< LO offset tuning (Hz), 0 = disabled
    bool   dc_offset_auto{true}; ///< hardware DC offset correction

    // Decode options
    bool soft_decode{false};   ///< soft-decision (LLR) Hamming decode (experimental)
    bool use_aa_filter{false}; ///< half-band FIR anti-alias in narrowband decimate
};

/// Scan-specific configuration (parsed from [scan] section).
/// Shares [device] and [radio_*] with TrxConfig; adds scan parameters.
struct ScanSetConfig {
    // From [device]
    std::string device{};
    std::string device_param{};
    double      l1_rate{16.0e6};      ///< L1 wideband sample rate
    double      master_clock{24.0e6}; ///< FPGA master clock rate (24 MHz fits under stock-UHD 2T2R ceiling of 30.72 MHz)
    std::string clock{};

    // From [radio_*]
    double freq_start{863.0e6}; ///< scan band start
    double freq_stop{870.0e6};  ///< scan band stop
    double gain{40.0};

    // From [scan]
    std::vector<uint32_t> bws{62500, 125000, 250000}; ///< BWs to probe
    uint32_t              os_factor{4};               ///< oversampling factor
    float                 min_ratio{5.0F};            ///< CAD detection threshold
    int                   settle_ms{5};               ///< PLL settle delay after retune (ms)
    uint32_t              sweeps{0};                  ///< 0 = infinite
    bool                  layer1_only{false};
    bool                  cbor_out{false}; ///< CBOR on stdout
    bool                  streaming{true}; ///< streaming pipeline (no retune)

    // Streaming-mode parameters
    float    buffer_ms{512.f};    ///< IQ ring buffer duration (ms)
    uint32_t l1_fft_size{4096};   ///< FFT bins for L1 energy
    float    channel_bw{62500.f}; ///< L1 channel grid spacing (Hz)

    // DC spur mitigation (from [radio_*])
    double lo_offset{0.0};
    bool   dc_offset_auto{true};

    // From [scan.network]
    std::string udp_listen{"127.0.0.1"};
    uint16_t    udp_port{5557}; ///< separate from lora_trx (5556)

    [[nodiscard]] double min_bw() const { return bws.empty() ? 62500.0 : static_cast<double>(*std::ranges::min_element(bws)); }

    [[nodiscard]] double center_freq() const { return (freq_start + freq_stop) / 2.0; }
};

// Shared RX status counters (updated by frame callback, read by main loop).
struct SharedStatus {
    std::atomic<uint32_t> frame_count{0};
    std::atomic<uint32_t> crc_ok{0};
    std::atomic<uint32_t> crc_fail{0};
    std::atomic<uint64_t> overflow_count{0};
};

// --- CLI + TOML config ---

void print_usage();

/// Parse CLI args.  Returns 0 on success, 1 on error, 2 on --help/--version.
/// `log_level` receives the --log-level value (empty if not specified).
int parse_args(int argc, char* argv[], std::string& config_path, std::string& log_level);

/// Load TOML config file into a vector of TrxConfig (one per set).
/// `cli_log_level` overrides [logging] level from TOML when non-empty.
/// "DEBUG" also enables block-level debug traces (cfg.debug = true).
/// Returns empty vector on error (messages printed to stderr).
std::vector<TrxConfig> load_config(const std::string& path, const std::string& cli_log_level = "");

/// Load TOML config file for scan mode.
/// Returns empty vector on error (messages printed to stderr).
std::vector<ScanSetConfig> load_scan_config(const std::string& path, const std::string& cli_log_level = "");

// --- Property map utilities ---

/// Merge override properties into a base property_map.
void merge_properties(gr::property_map& base, const gr::property_map& overrides);

/// Return only the entries whose key appears in `allowed`.
template<std::size_t N>
gr::property_map filter_properties(const gr::property_map& src, const std::array<const char*, N>& allowed) {
    gr::property_map out;
    for (const auto& [key, val] : src) {
        for (const auto* akey : allowed) {
            if (std::string_view(key) == akey) {
                out.insert_or_assign(std::pmr::string(std::string(key)), val);
                break;
            }
        }
    }
    return out;
}

// --- CBOR serialisation ---

/// Build a CBOR "config" message — sent once on subscribe.
/// Includes PHY, server, rx_chains, and raw passthrough sections.
/// `device_serial` is the SDR serial number (empty if unavailable).
std::vector<uint8_t> build_config_cbor(const TrxConfig& cfg, const std::string& device_serial = "");

/// Build a CBOR "status" heartbeat — sent periodically.
/// `git_rev` and `device_serial` identify the source binary and hardware.
std::vector<uint8_t> build_status_cbor(const TrxConfig& cfg, SharedStatus& status, const char* git_rev = "", const std::string& device_serial = "");

/// Build a CBOR spectrum message from a SpectrumState result buffer.
std::vector<uint8_t> build_spectrum_cbor(gr::lora::SpectrumState& spec, const char* type_name = "spectrum");

} // namespace lora_config

#endif // GR4_LORA_CONFIG_HPP
