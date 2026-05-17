// SPDX-License-Identifier: ISC
/// Tests for TOML configuration parsing (apps/config.cpp).
///
/// Suite 1: load_config — TRX mode parsing, field values, error cases
/// Suite 2: load_scan_config — scan mode parsing, validation, error cases

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <boost/ut.hpp>

#include "../apps/config.hpp"

#ifndef CONFIG_TOML_PATH
#define CONFIG_TOML_PATH "apps/config.toml"
#endif

using namespace boost::ut;

namespace {

const std::filesystem::path kTmpDir{"tmp"};

/// Write a string to a temp TOML file and return its path.
std::string write_tmp_toml(const std::string& name, const std::string& content) {
    std::filesystem::create_directories(kTmpDir);
    auto          path = (kTmpDir / name).string();
    std::ofstream ofs(path);
    ofs << content;
    return path;
}

} // namespace

// ── Suite 1: config load_config ────────────────────────────────────────────

const boost::ut::suite configLoadConfig = [] {
    "reference config loads"_test = [] {
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u) << "expected 1 TrxConfig";
        const auto& c = cfgs[0];

        // Device
        expect(eq(c.device, std::string{"uhd"}));
        expect(eq(c.device_param, std::string{"type=b200"}));
        expect(eq(c.clock, std::string{"external"}));

        // Radio
        expect(c.freq == 869'618'000.0) << "freq";
        expect(c.gain_rx == 40.0) << "gain_rx";
        expect(c.gain_tx == 73.5) << "gain_tx";
        expect(c.rx_channels.size() == 2_u) << "rx_channels count";
        expect(c.rx_channels[0] == 0_u) << "rx_channels[0]";
        expect(c.rx_channels[1] == 1_u) << "rx_channels[1]";
        expect(c.tx_channel == 0_u) << "tx_channel";

        // Codec
        expect(c.sf == 8_uc) << "sf";
        expect(c.bw == 62'500_u) << "bw";
        expect(c.cr == 4_uc) << "cr (8 - 4 = 4)";
        expect(that % c.sync == uint16_t{0x12}) << "sync_word";
        expect(c.preamble == 16_u) << "preamble (MeshCore RadioLib default)";

        // Rate & name
        expect(std::abs(c.rate - 250'000.f) < 1.f) << "rate";
        expect(eq(c.name, std::string{"MeshCore 868 MHz"}));

        // Network (from [trx.network])
        expect(eq(c.listen, std::string{"127.0.0.1"}));
        expect(c.port == 5556_u) << "port";
        expect(c.lbt == true) << "lbt";
    };

    "receive chain parsed (promiscuous default)"_test = [] {
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u);
        const auto& dc = cfgs[0].rx_chains;
        expect(dc.size() == 1_u) << "1 receive chain entry";
        expect(eq(dc[0].label, std::string{"promisc"}));
        expect(!dc[0].sync_word.has_value()) << "sync_word absent = promiscuous";
        expect(dc[0].sf_set.empty()) << "sf_set empty = sweep SF7-12";
    };

    "rx_bandwidths parsed"_test = [] {
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u);
        const auto& bws = cfgs[0].rx_bandwidths;
        expect(bws.size() == 1_u) << "1 RX bandwidth";
        expect(bws[0] == 62'500_u);
    };

    "network parsed"_test = [] {
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u);
        const auto& c = cfgs[0];
        expect(eq(c.listen, std::string{"127.0.0.1"}));
        expect(c.port == 5556_u);
        expect(c.lbt == true);
        expect(c.status_interval == 10_u);
    };

    "raw passthrough"_test = [] {
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u);
        const auto& r = cfgs[0].raw;
        // [meshcore] section is optional; fields default to empty when absent
        expect(r.meshcore.name.empty()) << "meshcore.name empty when [meshcore] absent";
        expect(r.aggregator.upstream.find("5556") != std::string::npos) << "aggregator.upstream contains 5556";
    };

    "cr validation"_test = [] {
        auto path = write_tmp_toml("bad_cr.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 3
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "cr=3 should fail validation";
    };

    "missing radio"_test = [] {
        auto path = write_tmp_toml("missing_radio.toml", R"(
[device]
driver = "uhd"

[trx]
radio = "radio_nonexistent"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "missing radio should fail";
    };

    "missing trx section"_test = [] {
        auto path = write_tmp_toml("no_trx.toml", R"(
[device]
driver = "uhd"
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "no [trx] should fail";
    };

    "missing receive section"_test = [] {
        auto path = write_tmp_toml("no_receive.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "no [trx.receive] should fail";
    };

    "empty bandwidths rejected"_test = [] {
        auto path = write_tmp_toml("empty_bws.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = []

[[trx.receive.chain]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "empty bandwidths should be rejected";
    };

    "missing chain entry"_test = [] {
        auto path = write_tmp_toml("no_chain.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "no [[trx.receive.chain]] should fail";
    };

    "sf list single parsed"_test = [] {
        auto path = write_tmp_toml("sf_single.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "pinned"
sync_word = 0x12
sf = [10]
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        expect(cfgs[0].rx_chains[0].sf_set.size() == 1_u) << "1 SF in set";
        expect(cfgs[0].rx_chains[0].sf_set[0] == 10_uc) << "sf[0] = 10";
    };

    "sf list non-contiguous parsed"_test = [] {
        auto path = write_tmp_toml("sf_subset.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "subset"
sync_word = 0x12
sf = [8, 11]
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        const auto& s = cfgs[0].rx_chains[0].sf_set;
        expect(s.size() == 2_u) << "2 SFs in set";
        expect(s[0] == 8_uc);
        expect(s[1] == 11_uc);
    };

    "sf list out of range rejected"_test = [] {
        auto path = write_tmp_toml("sf_bad.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "bad"
sync_word = 0x12
sf = [5]
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "sf = [5] (out of [7..12]) should fail";
    };

    "sf list duplicate rejected"_test = [] {
        auto path = write_tmp_toml("sf_dup.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "dup"
sync_word = 0x12
sf = [8, 8]
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "sf = [8, 8] (duplicate) should fail";
    };

    "promiscuous chain (no sync_word)"_test = [] {
        auto path = write_tmp_toml("promisc.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "any"
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        expect(!cfgs[0].rx_chains[0].sync_word.has_value()) << "sync_word absent = promiscuous";
    };

    "multi-chain parsed"_test = [] {
        auto path = write_tmp_toml("multi_chain.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "meshcore"
sync_word = 0x12

[[trx.receive.chain]]
label = "lorawan"
sync_word = 0x34
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        const auto& chains = cfgs[0].rx_chains;
        expect(chains.size() == 2_u) << "2 chains";
        expect(eq(chains[0].label, std::string{"meshcore"}));
        expect(eq(chains[1].label, std::string{"lorawan"}));
        expect(chains[0].sync_word.has_value());
        expect(chains[1].sync_word.has_value());
        expect(that % *chains[0].sync_word == uint16_t{0x12});
        expect(that % *chains[1].sync_word == uint16_t{0x34});
    };

    "duplicate chain label rejected"_test = [] {
        auto path = write_tmp_toml("dup_label.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "meshcore"
sync_word = 0x12

[[trx.receive.chain]]
label = "meshcore"
sync_word = 0x34
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "duplicate label should fail";
    };

    "duplicate chain sync_word permitted"_test = [] {
        // Two chains with the same sync_word but different block_overrides
        // (e.g. min_snr_db) is a valid tuning pattern — strict snr vs. lax
        // snr on the same protocol.  Label uniqueness is the only rule.
        auto path = write_tmp_toml("dup_sync.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "strict"
sync_word = 0x12
min_snr_db = -5.0

[[trx.receive.chain]]
label = "lax"
sync_word = 0x12
min_snr_db = -15.0
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u) << "duplicate sync_word with distinct labels should parse";
        expect(cfgs[0].rx_chains.size() == 2_u);
    };

    "duplicate bandwidth permitted"_test = [] {
        // Permitted (permissive).  Wasteful but user's call.
        auto path = write_tmp_toml("dup_bw.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000, 125000]

[[trx.receive.chain]]
label = "a"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u) << "duplicate BW should parse (permissive)";
    };

    "legacy schema rejected"_test = [] {
        auto path = write_tmp_toml("legacy.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[codec_test]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx]
radio = "radio_test"
codec = "codec_test"

[[trx.decode]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.empty()) << "legacy [codec_*] + [[trx.decode]] schema should be rejected";
    };

    "enable_tx defaults to true"_test = [] {
        // Reference config has no enable_tx key → should default to true.
        auto cfgs = lora_config::load_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u);
        expect(cfgs[0].enable_tx == true) << "enable_tx should default to true when omitted";
    };

    "enable_tx = false parsed"_test = [] {
        auto path = write_tmp_toml("enable_tx_false.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"
enable_tx = false

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        expect(cfgs[0].enable_tx == false) << "enable_tx = false should be parsed";
    };

    "enable_tx = true explicit"_test = [] {
        auto path = write_tmp_toml("enable_tx_true.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[trx]
radio = "radio_test"
enable_tx = true

[trx.transmit]
sf = 8
bw = 125000
cr = 8
sync_word = 0x12
preamble_len = 8

[trx.receive]
bandwidths = [125000]

[[trx.receive.chain]]
label = "test"
sync_word = 0x12
)");
        auto cfgs = lora_config::load_config(path);
        expect(cfgs.size() == 1_u);
        expect(cfgs[0].enable_tx == true) << "enable_tx = true explicit should be parsed";
    };
};

// ── Suite 2: config load_scan_config ───────────────────────────────────────

const boost::ut::suite configLoadScan = [] {
    "reference scan config loads"_test = [] {
        auto cfgs = lora_config::load_scan_config(CONFIG_TOML_PATH);
        expect(cfgs.size() == 1_u) << "expected 1 ScanSetConfig";
        const auto& s = cfgs[0];

        expect(s.freq_start == 867'000'000.0) << "freq_start";
        expect(s.freq_stop == 870'000'000.0) << "freq_stop";
        expect(s.bws.size() >= 1_u);
        expect(std::ranges::find(s.bws, uint32_t{62500}) != s.bws.end()) << "bws contains 62500";
        expect(s.os_factor == 4_u) << "os_factor";
        expect(std::abs(s.min_ratio - 5.0F) < 0.01F) << "min_ratio";
        expect(s.l1_rate == 4'000'000.0) << "l1_rate";
        expect(s.master_clock == 8'000'000.0) << "master_clock";
        expect(s.udp_port == 5557_u) << "udp_port";
    };

    "missing scan section"_test = [] {
        auto path = write_tmp_toml("no_scan.toml", R"(
[device]
driver = "uhd"
)");
        auto cfgs = lora_config::load_scan_config(path);
        expect(cfgs.empty()) << "no [scan] should fail";
    };

    "bad decimation"_test = [] {
        auto path = write_tmp_toml("bad_decim.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[scan]
radio      = "radio_test"
freq_start = 863000000
freq_stop  = 870000000
bws        = [62500]
l1_rate    = 10000000
os_factor  = 3
)");
        // 10e6 / (62500 * 3) = 10e6 / 187500 = 53.33 — not integer
        auto cfgs = lora_config::load_scan_config(path);
        expect(cfgs.empty()) << "non-integer decimation should fail";
    };

    "freq_start >= freq_stop"_test = [] {
        auto path = write_tmp_toml("bad_freq.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[scan]
radio      = "radio_test"
freq_start = 870000000
freq_stop  = 863000000
bws        = [125000]
l1_rate    = 16000000
os_factor  = 4
)");
        auto cfgs = lora_config::load_scan_config(path);
        expect(cfgs.empty()) << "freq_start >= freq_stop should fail";
    };

    "bws sorted"_test = [] {
        auto path = write_tmp_toml("unsorted_bws.toml", R"(
[device]
driver = "uhd"

[radio_test]
freq = 868000000

[scan]
radio      = "radio_test"
freq_start = 863000000
freq_stop  = 870000000
bws        = [250000, 62500]
l1_rate    = 16000000
os_factor  = 4
)");
        auto cfgs = lora_config::load_scan_config(path);
        expect(cfgs.size() == 1_u) << "should parse OK";
        expect(cfgs[0].bws.size() == 2_u);
        expect(cfgs[0].bws[0] == 62'500_u) << "bws[0] should be smallest";
        expect(cfgs[0].bws[1] == 250'000_u) << "bws[1] should be largest";
    };
};

int main() {}
