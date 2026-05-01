# CBOR Schemas

All inter-process communication uses [CBOR (RFC 8949)](https://cbor.io/) —
a compact binary encoding of JSON-like data. Every message is a CBOR map with
a text key `"type"` that identifies the frame type. Recipients dispatch on
this string value.

**C++ encoder/decoder:** `blocks/include/gnuradio-4.0/lora/cbor.hpp` — a
minimal, hand-rolled single-header implementation. Encodes to / decodes from
`std::vector<uint8_t>`. The decoder only handles the top-level map; nested
maps are produced but never consumed on the C++ side.

**Telemetry encoder:** `blocks/include/gnuradio-4.0/lora/algorithm/Telemetry.hpp`
— generic `property_map` → CBOR encoder. Blocks emit `property_map` events;
the app injects a lambda that serialises via `telemetry::encode()` and
broadcasts over UDP. Auto-adds ISO 8601 `"ts"` field.

**Python:** `cbor2` (`pip install cbor2`).

**Wireshark:** `scripts/wireshark/lora_trx.lua` — dissects all frame types on UDP 5555.

## Frame types — lora_trx

| Type | Direction | Source | Port |
|------|-----------|--------|------|
| [`lora_frame`](#lora_frame) | server → clients | `FrameSink.hpp` | 5555 |
| [`lora_tx`](#lora_tx) | client → server | any CBOR client | 5555 |
| [`lora_tx_ack`](#lora_tx_ack) | server → requesting client | `lora_trx.cpp` | 5555 |
| [`subscribe`](#subscribe) | client → server | any CBOR client | 5555 |
| [`config`](#config) | server → client (on subscribe) | `config.cpp` | 5555 |
| [`status`](#status) | server → all clients (periodic) | `config.cpp` | 5555 |
| [`spectrum`](#spectrum) | server → all clients | `config.cpp` | 5555 |
| [`spectrum_tx`](#spectrum_tx) | server → all clients | `config.cpp` | 5555 |

Runtime PHY reconfiguration was removed.  The old `lora_config` /
`lora_config_ack` message pair is no longer supported — edit
`apps/config.toml` and restart `lora_trx` to change SF / BW /
sync_word / preamble / freq / gain.  A request with `type =
"lora_config"` still produces a `lora_config_ack` but the ack now
always carries `ok = false` and `error = "runtime_phy_reconfig_removed"`.

## Frame types — lora_trx telemetry

| Type | Direction | Source | Port |
|------|-----------|--------|------|
| [`multisf_detect`](#multisf_detect) | server → clients | `MultiSfDecoder.hpp` | 5555 |
| [`multisf_sync`](#multisf_sync) | server → clients | `MultiSfDecoder.hpp` | 5555 |
| [`multisf_frame`](#multisf_frame) | server → clients | `MultiSfDecoder.hpp` | 5555 |
| [`wideband_sweep`](#wideband_sweep) | server → clients | `WidebandDecoder.hpp` | 5555 |
| [`wideband_slot`](#wideband_slot) | server → clients | `WidebandDecoder.hpp` | 5555 |

## Frame types — lora_scan

| Type | Direction | Source | Mode |
|------|-----------|--------|------|
| [`scan_spectrum`](#scan_spectrum) | broadcast | `lora_scan.cpp` | both |
| [`scan_sweep_start`](#scan_sweep_start) | broadcast | `lora_scan.cpp` | legacy |
| [`scan_sweep_end`](#scan_sweep_end) | broadcast | `lora_scan.cpp` | both |
| [`scan_l2_probe`](#scan_l2_probe) | broadcast | `lora_scan.cpp` | legacy |
| [`scan_retune`](#scan_retune) | broadcast | `lora_scan.cpp` | legacy |
| [`scan_overflow`](#scan_overflow) | broadcast | `lora_scan.cpp` | legacy |
| [`scan_status`](#scan_status) | broadcast | `lora_scan.cpp` | legacy |

`lora_scan` has two modes: **legacy** (hardware retune sweep) and **streaming**
(wideband digital channelisation). In streaming mode, only `scan_spectrum` and
`scan_sweep_end` are emitted (via `emitDataSetCbor()`).

---

## `lora_frame`

Decoded RX frame. Produced by `FrameSink::buildFrameCbor()` once per decode
event, delivered to clients via `lora_trx` UDP broadcast.

```
{
  "type":         "lora_frame",       // text
  "ts":           "2026-02-17T...",   // text, ISO 8601 UTC
  "seq":          1,                  // uint, 1-based frame counter (per FrameSink)
  "phy": {                            // map (variable field count)
    "sf":             8,              // uint, spreading factor
    "bw":             62500,          // uint, bandwidth (Hz)
    "cr":             4,              // uint, coding rate (1–4)
    "crc_valid":      true,           // bool
    "sync_word":      18,             // uint (0x12 = 18)
    "snr_db":         12.3,           // float64, FFT-domain SNR estimate (dB)
    "noise_floor_db": -42.1,          // float64, optional, EMA noise floor (dBFS)
    "peak_db":        -6.2,           // float64, optional, EMA peak amplitude (dBFS)
    "snr_db_td":      -7.2,           // float64, optional, time-domain SNR (dB)
    "channel_freq":   869618000.0,    // float64, optional, channel center freq (Hz)
    "decode_bw":      62500.0,        // float64, optional, decode bandwidth (Hz)
    "cfo_int":        3.0,            // float64, optional, integer CFO (bins)
    "cfo_frac":       0.214,          // float64, optional, fractional CFO
    "sfo_hat":        -0.001,         // float64, optional, sample freq offset estimate
    "sample_rate":         250000.0,  // float64, optional, effective RX sample rate (Hz)
    "frequency_corrected": 869618000.0, // float64, optional, ppm-corrected RX freq (Hz)
    "ppm_error":           1.2,       // float64, optional, clock drift estimate (ppm)
    "ldro_detected":       false,     // bool, optional, statistical LDRO detection
    "ldro_match":          true,      // bool, optional, cfg vs detected agree
    "ldro_lsb0_fraction":  0.5,       // float64, optional, fraction with adjustBin&1==0
    "ldro_n_samples":      40         // uint, optional, payload symbols tested
  },
  "carrier": {                        // map, 5 keys (RX) / 5 keys (TX)
    "sync_word":      18,             // uint (0x12 = 18)
    "sf":             8,              // uint, spreading factor
    "bw":             62500,          // uint, bandwidth (Hz)
    "cr":             4,              // uint, coding rate (1–4)
    "ldro_cfg":       false           // bool, rule-based LDRO flag (symbol_time > 16 ms)
  },
  "payload":      h'48656C6C6F',      // bytes, raw payload
  "payload_len":  5,                  // uint
  "crc_valid":    true,               // bool (top-level duplicate)
  "cr":           4,                  // uint (top-level duplicate)
  "is_downchirp": false,              // bool
  "rx_channel":   0,                  // uint, optional, RX channel index
  "decode_label": "meshcore",         // text, optional, decode chain label
  "id":           "550e8400-...",      // text, UUIDv4, unique per decode event
  "payload_hash": 12345678901234,     // uint, FNV-1a 64-bit hash
  "device":       "31DE7F5"           // text, optional, SDR device serial
}
```

**Optional `phy` fields:** `noise_floor_db`, `peak_db`, `snr_db_td` are only
present when their EMA values have been initialised (value > -999).
`channel_freq`, `decode_bw`, `cfo_int`, `cfo_frac`, `sfo_hat` are only
present when decoded by `WidebandDecoder` (channel_freq > 0).
`sample_rate`, `frequency_corrected`, `ppm_error` are only present when
the upstream `SoapySource` `ppm_estimator` has emitted a drift-tracking
tag since the last frame (drift > `ppm_tag_threshold`). `sample_rate`
stays float32 on the GR tag wire (SigMF `tag::SAMPLE_RATE` contract) and
is widened to float64 at the CBOR emission boundary. `frequency_corrected`
is the ppm-adjusted RX centre frequency (`nominal * (1 + ppm_error * 1e-6)`);
distinct from `channel_freq` (nominal, set by `WidebandDecoder`).
`ldro_*` are only present when DecodeChain collected ≥ 8 payload
symbols; all four appear together or not at all.

**LDRO semantics:** `carrier.ldro_cfg` is the rule-based Semtech flag the
decoder used (`symbol_time > 16 ms`). `phy.ldro_detected` is a statistical
test on the raw payload bins: under LDRO, the interleaver packs each
symbol MSB-first as `[data | parity | zero]`, so `adjustBin(bin) & 1`
is ALWAYS zero. `phy.ldro_lsb0_fraction` near 1.0 ⇒ LDRO on, near 0.5
⇒ LDRO off. `phy.ldro_detected` trips at `fraction ≥ 0.75` and
requires `ldro_n_samples ≥ 16` for statistical power (Bin(n, 0.5)
false-positive rate < 1% at that threshold). `phy.ldro_match == false`
is an alarm: decoder used the wrong interleaver shape for this frame —
the CRC almost certainly failed.

**Optional top-level fields:** `rx_channel` requires multi-channel config.
`decode_label` requires a non-empty label in TOML. `device` requires the SDR
to report a serial number.

**SNR fields:** `snr_db` is an FFT-domain estimate (peak-vs-rest energy ratio
averaged over preamble symbols) and includes ~SF×3 dB of FFT processing gain.
`snr_db_td` is a time-domain estimate (`signal_db - noise_floor_db`) that
normalises the FFT peak back to per-sample power, making it comparable to
RSSI-based SNR values reported by companion devices.

**Consumers:** `lora.viz.mon`, `lora-core`, `lora.viz.waterfall`,
`lora.storage.duckdb_writer`, `lora.hwtests.trx_perf`, `trx_test.py`, `trx_ab_loop.py`,
`test_multisf_hardware.py`, `lora.tools.decode meshcore`, `lora.bridges.meshcore`,
`lora_trx.lua`.

### Aggregated frame (from lora-core on :5555)

Same as `lora_frame`, plus a `diversity` sub-map added by `lora-core`:

```
{
  // ... all lora_frame fields ...
  "diversity": {                      // map, added by lora-core
    "n_candidates":    2,             // uint, decode chains that decoded this frame
    "decoded_channel": 1,             // uint, rx_channel of the winning decode
    "rx_channels":     [0, 1],        // uint array, per-candidate rx_channel
    "snr_db":          [3.2, 5.1],    // float64 array, per-candidate SNR
    "crc_mask":        3,             // uint, bitmask of CRC-OK candidates
    "gap_us":          20000,         // uint, microseconds from first to last candidate
    "source_ids":      ["uuid1", ...] // text array, UUID of each raw candidate
  }
}
```

---

## `lora_tx`

TX request sent from a client to `lora_trx`. Triggers transmission of the
payload using the server's current PHY parameters, with optional overrides
for coding rate, sync word, and preamble length.

```
{
  "type":         "lora_tx",          // text, required
  "payload":      h'48656C6C6F',      // bytes, required, 1–255 bytes
  "seq":          1,                  // uint, optional, echoed in lora_tx_ack
  "cr":           4,                  // uint, optional, coding rate (1–4)
  "sync_word":    18,                 // uint, optional (0x12 = 18)
  "preamble_len": 8,                  // uint, optional, preamble symbols
  "repeat":       1,                  // uint, optional, transmit count
  "gap_ms":       1000,               // uint, optional, gap between repeats (ms)
  "dry_run":      false               // bool, optional, skip actual transmission
}
```

**Note:** `lora_trx` only reads the fields listed above. Any additional keys
(e.g., `freq`, `sf`, `bw`) are silently ignored. PHY parameters are
startup-only — edit `apps/config.toml` and restart `lora_trx` to change them.

**Producers:** `meshcore_tx.py`, `lora.bridges.meshcore`, `lora-core` (proxy).

---

## `lora_tx_ack`

Sent back to the requesting client after a `lora_tx` request is processed
(or rejected).

```
{
  "type":   "lora_tx_ack",            // text
  "seq":    1,                        // uint, echoed from lora_tx request
  "ok":     true,                     // bool, false on error
  "error":  "channel_busy"            // text, optional, reason for rejection
}
```

**Error values:** `"channel_busy"` (LBT timeout), `"tx_queue_full"` (queue at
capacity), `"tx_disabled"` (TX graph not built — `enable_tx=false`),
`"internal"` (uncaught exception in the TX worker, e.g. malformed CBOR).
The `error` field is only present when `ok` is `false`.

**Consumers:** `lora-core` (pass-through), `lora.viz.waterfall` (records TX
time), `lora_trx.lua`.

---

## `subscribe`

Sent to `lora_trx` (or `lora-core`) to register as a client and optionally
filter frames by sync word. Re-sending acts as a keepalive and updates the
filter. Any non-CBOR datagram also registers the sender (no filter — receives
all frames). Dead clients (10 consecutive `sendto` failures) are pruned.

```
{
  "type":       "subscribe",          // text, required
  "sync_word":  [18]                  // uint array, optional, filter by sync word
}
```

`sync_word` values: `0x12` (18) = MeshCore/Reticulum, `0x2B` (43) =
Meshtastic, `0x34` (52) = LoRaWAN. Omit the field to receive all frames.

**Producers:** `lora_common.py` (used by `lora.viz.mon`, `lora.viz.waterfall`,
`lora.storage.duckdb_writer`), `lora-core` (upstream keepalive), `lora.bridges.meshcore`.

---

## `config`

Sent once to a client immediately after it subscribes.  Contains the
current PHY parameters (TX preset only), server metadata, per-chain RX
configuration, and passthrough config for Python scripts.  Runtime
reconfiguration is not supported — this message is only emitted on
initial subscription.

```
{
  "type":  "config",                  // text
  "phy": {                            // map, 8 keys
    "freq":       869618000.0,        // float64, center frequency (Hz)
    "sf":         8,                  // uint, spreading factor
    "bw":         62500,              // uint, bandwidth (Hz)
    "cr":         4,                  // uint, coding rate (1–4)
    "sync_word":  18,                 // uint (0x12 = 18)
    "preamble":   8,                  // uint, preamble symbols
    "rx_gain":    40.0,               // float64, RX gain (dB)
    "tx_gain":    74.0                // float64, TX gain (dB)
  },
  "server": {                         // map, 3-4 keys
    "device":          "uhd",         // text, SDR backend
    "status_interval": 10,            // uint, seconds between status heartbeats (0 = off)
    "sample_rate":     250000.0,      // float64, sample rate (S/s)
    "device_serial":   "31DE7F5"      // text, optional, SDR device serial (EEPROM)
  },
  "rx_chains": [                      // array of maps, one per [[trx.receive.chain]]
    {
      "label":       "any",           // text, chain label (unique across chains)
      "promiscuous": true,            // bool, true when sync_word absent in TOML
      "sync_word":   18,              // uint, optional — present only when strict
                                      //                filter is configured
      "sf":          []               // array<uint>, SF subset ([] = sweep SF7-12,
                                      //                [N] = pin, [N1,N2,…] = subset)
    }
  ],
  "raw": {                            // map, passthrough config for Python scripts
    "network": {                      // map, [network] TOML section
      "udp_listen":      "::",        // text, UDP listen address
      "udp_port":        5555,        // uint, UDP port
      "status_interval": 10,         // uint, seconds between status heartbeats
      "lbt":             true,        // bool, listen-before-talk enabled
      "lbt_timeout_ms":  2000,        // uint, max wait for channel clear (ms)
      "tx_queue_depth":  16           // uint, max queued TX requests
    },
    "aggregator": {                   // map, [aggregator] TOML section
      "upstream":   "127.0.0.1:5556", // text, lora_trx raw port (host:port)
      "listen":     ":::5555",        // text, consumer-facing bind address (host:port)
      "window_ms":  100               // uint, diversity combining window (ms)
    },
    "meshcore": {                     // map, [meshcore] TOML section
      "name":             "node1",    // text, optional, node display name
      "model":            "B210",     // text, optional, device model string
      "identity_file":    "id.bin",   // text, optional, path to identity file
      "keys_dir":         "keys/",    // text, optional, path to keys directory
      "channels_dir":     "chan/",    // text, optional, path to channels directory
      "contacts_dir":     "cont/",   // text, optional, path to contacts directory
      "lat":              48.123,     // float64, optional, latitude
      "lon":              11.456,     // float64, optional, longitude
      "port":             4200,       // uint, TCP listen port
      "region_scope":     "de-nord",  // text, optional, region scope hashtag
      "flood_scope":      "local",    // text, optional, flood scope string
      "client_repeat":    0,          // uint, 0=off, 1=on
      "path_hash_mode":   0,          // uint, 0-2
      "autoadd_config":   0,          // uint, bitmask
      "autoadd_max_hops": 4           // uint, 0-64
    },
    "logging": {                      // map, [logging] TOML section
      "level":  "INFO",              // text, log level (DEBUG, INFO, WARNING, ERROR)
      "color":  true                  // bool, ANSI color enabled
    }
  }
}
```

**Optional fields in `raw.meshcore`:** `name`, `model`, `identity_file`,
`keys_dir`, `channels_dir`, `contacts_dir`, `region_scope`, `flood_scope`
are omitted when the string is empty. `lat` and `lon` are omitted when zero.

**Consumers:** `lora.viz.mon`, `lora.viz.waterfall`, `lora.bridges.meshcore`,
`lora_trx.lua`.

---

## `status`

Broadcast periodically to **all** connected clients (ignores sync_word
filters). Interval is configured via `status_interval` in `config.toml`
(default 10 seconds, 0 = disabled).

```
{
  "type":         "status",           // text
  "ts":           "2026-02-25T...",   // text, ISO 8601 UTC
  "phy": {                            // map, 2 keys
    "rx_gain":    40.0,               // float64, RX gain (dB)
    "tx_gain":    74.0                // float64, TX gain (dB)
  },
  "frames": {                         // map, 3 keys
    "total":      42,                 // uint, total frames since startup
    "crc_ok":     38,                 // uint, frames with valid CRC
    "crc_fail":   4                   // uint, frames with CRC failure
  },
  "rx_overflows": 0                   // uint, cumulative SoapySDR overflow count
}
```

**Note:** `rx_overflows` is a top-level key, not inside `frames`.

**Consumers:** `lora.viz.mon`, `lora.viz.waterfall`, `lora.bridges.meshcore`,
`lora_trx.lua`.

---

## `spectrum`

RX waterfall data. Broadcast to all connected clients when spectrum data is
available. Generated by `SpectrumTap` in `FrameSync` during DETECT state.
Bins are float32 LE magnitude values in dBFS, DC-centred (fftshift applied).

```
{
  "type":         "spectrum",         // text
  "bins":         h'...',             // bytes, fft_size × float32 LE, dBFS
  "fft_size":     1024,               // uint, number of FFT bins
  "center_freq":  869618000.0,        // float64, center frequency (Hz)
  "sample_rate":  250000.0            // float64, sample rate (S/s)
}
```

**Consumers:** `lora.viz.waterfall`, `lora.cal.dc_diag`, `lora_trx.lua`.

---

## `spectrum_tx`

TX waterfall data. Same schema as `spectrum` except `"type": "spectrum_tx"`.
Generated from TX IQ data during transmission, allowing the waterfall to show
TX bursts alongside the RX spectrum.

```
{
  "type":         "spectrum_tx",      // text
  "bins":         h'...',             // bytes, fft_size × float32 LE, dBFS
  "fft_size":     1024,               // uint
  "center_freq":  869618000.0,        // float64 (Hz)
  "sample_rate":  250000.0            // float64 (S/s)
}
```

**Consumers:** `lora_trx.lua`. Python clients receive this type but
`lora.viz.waterfall` currently only renders `spectrum` (RX).

---

## `lora_config` / `lora_config_ack` — REMOVED

The runtime PHY reconfiguration API was removed when multi-chain RX
landed: broadcasting one set of SF/BW/sync_word/preamble values to all
decoders no longer expresses what the user means when multiple chains
exist (different sync words, different SF subsets, different block
overrides per chain).

`lora_trx` still answers `type = "lora_config"` requests with a
`lora_config_ack`, but the ack always carries:

```
{
  "type":  "lora_config_ack",
  "ok":    false,
  "error": "runtime_phy_reconfig_removed"
}
```

---

## `multisf_detect`

Emitted when a preamble is detected (DETECT → SYNC state transition) on any
SF lane in the `MultiSfDecoder` block. Encoded via `Telemetry::encode()`.

```
{
  "type":  "multisf_detect",          // text
  "ts":    "2026-03-22T...",          // text, ISO 8601 UTC (auto-added)
  "sf":    8,                         // uint, spreading factor that detected
  "bin":   42                         // uint, dechirp bin index of last preamble upchirp
}
```

**Consumers:** `lora.hwtests.trx_perf`.

---

## `multisf_sync`

Emitted when sync word is verified and SNR passes threshold (SYNC → OUTPUT
state transition) in `MultiSfDecoder`. Encoded via `Telemetry::encode()`.

```
{
  "type":     "multisf_sync",         // text
  "ts":       "2026-03-22T...",       // text, ISO 8601 UTC (auto-added)
  "sf":       8,                      // uint, spreading factor
  "cfo_int":  -2,                     // int, integer carrier freq offset (bins)
  "cfo_frac": 0.214,                  // float64, fractional carrier freq offset
  "snr_db":   12.3                    // float64, signal-to-noise ratio (dB)
}
```

**Consumers:** `lora.hwtests.trx_perf`.

---

## `multisf_frame`

Emitted when a complete frame is decoded (all symbols received and processed)
in `MultiSfDecoder`. Encoded via `Telemetry::encode()`.

```
{
  "type":    "multisf_frame",         // text
  "ts":      "2026-03-22T...",        // text, ISO 8601 UTC (auto-added)
  "sf":      8,                       // uint, spreading factor
  "crc_ok":  true,                    // bool, CRC check result
  "len":     118,                     // uint, payload length (bytes)
  "cr":      4,                       // uint, coding rate (0–4)
  "snr_db":  12.3                     // float64, signal-to-noise ratio (dB)
}
```

**Consumers:** `lora.hwtests.trx_perf`.

---

## `wideband_sweep`

Emitted every L1 sweep completion in `WidebandDecoder` (wideband mode only,
`wideband=true` in config). Encoded via `Telemetry::encode()`.

```
{
  "type":        "wideband_sweep",    // text
  "ts":          "2026-03-22T...",    // text, ISO 8601 UTC (auto-added)
  "sweep":       42,                  // uint, sweep sequence number
  "tainted":     false,               // bool, overflows exceeded threshold
  "overflows":   0,                   // uint, overflow events during this sweep
  "zero_calls":  2,                   // uint, processBulk calls with 0 input
  "total_calls": 1024,               // uint, total processBulk calls this sweep
  "duration_ms": 524,                 // uint, sweep duration (ms)
  "n_snapshots": 16,                  // uint, L1 FFT snapshots taken
  "n_hot":       1,                   // uint, hot channels detected
  "n_active":    1,                   // uint, active ChannelSlots
  "max_slots":   24                   // uint, maximum ChannelSlots (config)
}
```

**Consumers:** `lora.hwtests.trx_perf`.

---

## `wideband_slot`

Emitted when a ChannelSlot is activated or deactivated in `WidebandDecoder`
(wideband mode only). Encoded via `Telemetry::encode()`.

```
{
  "type":    "wideband_slot",         // text
  "ts":      "2026-03-22T...",        // text, ISO 8601 UTC (auto-added)
  "action":  "activate",             // text, "activate" or "deactivate"
  "slot":    0,                       // uint, slot index (0 to max_channels-1)
  "channel": 42,                      // uint, L1 channel index
  "freq":    869618000.0,             // float64, channel center frequency (Hz)
  "bw":      62500                    // uint, decode bandwidth (Hz)
}
```

**Consumers:** `lora.hwtests.trx_perf`.

---

## `scan_spectrum`

L1 energy spectrum with L2 detection results. Emitted once per sweep.

In **legacy** mode, produced by `emitSpectrumCbor()` (emitted after
`scan_sweep_end`). In **streaming** mode, produced by `emitDataSetCbor()`
(emitted before `scan_sweep_end`).

```
{
  "type":       "scan_spectrum",      // text
  "ts":         "2026-03-11T...",     // text, ISO 8601 UTC
  "sweep":      42,                   // uint, 1-based sweep counter
  "freq_min":   863000000,            // uint, first channel center freq (Hz)
  "freq_step":  62500,                // uint, channel spacing (Hz)
  "channels":   h'...',              // bytes, n_channels × float32 LE, linear power
  "n_channels": 112,                  // uint, number of channels
  "hot":        [5, 42, 73],          // uint array, hot channel indices (0-based)
  "detections": [...]                 // array of detection sub-maps (see below)
}
```

**Detection sub-map (legacy mode, 7 fields):**

```
{
  "freq":     869618000,              // uint, detection frequency (Hz)
  "sf":       8,                      // uint, detected spreading factor (7–12)
  "bw":       62500,                  // uint, detection bandwidth (Hz)
  "ratio":    35.5,                   // float64, max(ratio_up, ratio_dn)
  "ratio_up": 35.5,                   // float64, upchirp peak ratio
  "ratio_dn": 1.2,                    // float64, downchirp peak ratio
  "chirp":    "up"                    // text, "up", "dn", or "both"
}
```

**Detection sub-map (streaming mode, 4 fields):**

```
{
  "freq":   869618000,                // uint, detection frequency (Hz)
  "sf":     8,                        // uint, detected spreading factor
  "bw":     62500,                    // uint, detection bandwidth (Hz)
  "ratio":  35.5                      // float64, peak ratio
}
```

**Consumers:** `lora.viz.spectrum`, `lora.hwtests.scan_perf`, `lora.hwtests.scan_test`.

---

## `scan_sweep_start`

Emitted at the beginning of each sweep. **Legacy mode only.**

```
{
  "type":   "scan_sweep_start",       // text
  "ts":     "2026-03-11T...",         // text, ISO 8601 UTC
  "sweep":  42                        // uint, 1-based sweep number
}
```

**Consumers:** `lora.viz.spectrum`.

---

## `scan_sweep_end`

Emitted at the end of each sweep, after all L1/L2 work completes.

**Legacy mode (9 fields):**

```
{
  "type":         "scan_sweep_end",   // text
  "ts":           "2026-03-11T...",   // text, ISO 8601 UTC
  "sweep":        42,                 // uint, 1-based sweep number
  "duration_ms":  1063,               // uint, wall-clock sweep duration (ms)
  "l1_snapshots": 678,                // uint, L1 FFT snapshots taken
  "l2_probes":    3,                  // uint, L2 CAD probes performed
  "hot_count":    3,                  // uint, hot channels detected
  "detections":   2,                  // uint, positive detections
  "overflows":    0                   // uint, overflow events this sweep
}
```

**Streaming mode (8 fields):** same as legacy but **without `l2_probes`**.
`overflows` is currently hardcoded to 0 (TODO: not wired in streaming mode).

**Consumers:** `lora.hwtests.scan_perf`, `lora.viz.spectrum`, `lora.hwtests.scan_test`.

---

## `scan_l2_probe`

Emitted after each L2 CAD probe of a hot channel. **Legacy mode only.**

```
{
  "type":        "scan_l2_probe",     // text
  "ts":          "2026-03-11T...",    // text, ISO 8601 UTC
  "sweep":       42,                  // uint, current sweep number
  "freq":        869618000,           // uint, probed channel freq (Hz)
  "duration_ms": 134,                 // uint, probe duration (ms)
  "results":     [...]                // array of per-BW result sub-maps
}
```

**Per-BW result sub-map (4 fields):**

```
{
  "bw":     62500,                    // uint, bandwidth probed (Hz)
  "sf":     8,                        // uint, detected spreading factor
  "ratio":  35.5,                     // float64, peak-to-median ratio
  "chirp":  "up"                      // text, "up", "dn", or "both"
}
```

**Consumers:** `lora.viz.spectrum`.

---

## `scan_retune`

Emitted when the SDR retunes to a new center frequency. **Legacy mode only.**

```
{
  "type":    "scan_retune",           // text
  "ts":      "2026-03-11T...",        // text, ISO 8601 UTC
  "sweep":   42,                      // uint, current sweep number
  "freq":    869618000,               // uint, new center frequency (Hz)
  "reason":  "l2"                     // text, retune reason (e.g. "l1", "l2")
}
```

---

## `scan_overflow`

Emitted when SoapySDR overflow count increases. **Legacy mode only.**

```
{
  "type":   "scan_overflow",          // text
  "ts":     "2026-03-11T...",         // text, ISO 8601 UTC
  "sweep":  42,                       // uint, current sweep number
  "count":  5                         // uint, cumulative total overflow count
}
```

**Consumers:** `lora.hwtests.scan_perf`, `lora.viz.spectrum`.

---

## `scan_status`

Periodic status summary. Emitted every 5 seconds and on first sweep
completion. **Legacy mode only.**

```
{
  "type":       "scan_status",        // text
  "ts":         "2026-03-11T...",     // text, ISO 8601 UTC
  "mode":       "streaming",         // text, scan mode identifier
  "sweeps":     42,                   // uint, total sweeps completed
  "detections": 15,                   // uint, total cumulative detections
  "overflows":  0,                    // uint, total overflow events
  "dropouts":   0,                    // uint, total sample dropouts
  "scan_range": [863000000, 870000000] // uint array, [freq_start, freq_stop] (Hz)
}
```

**Consumers:** `lora.viz.spectrum`.

---

## Emission ordering

### lora_trx — per decoded frame

1. `multisf_detect` (preamble found)
2. `multisf_sync` (sync word verified)
3. `multisf_frame` (decode complete)
4. `lora_frame` (delivered to consumers)

### lora_scan — per sweep (legacy mode)

1. `scan_sweep_start`
2. `scan_retune` (0..N, per retune)
3. `scan_l2_probe` (0..N, per hot channel)
4. `scan_overflow` (0..N, if overflows detected)
5. `scan_sweep_end`
6. `scan_spectrum`
7. `scan_status` (conditional: first sweep, or every 5s)

### lora_scan — per sweep (streaming mode)

1. `scan_spectrum`
2. `scan_sweep_end`

---

## UDP protocol

`lora_trx` exposes a single UDP port (default 5555, IPv6 dual-stack) for both
RX frame output and TX/config input.

### Client subscription

Clients send a CBOR `subscribe` message to register and set a sync word
filter. Any non-CBOR datagram also registers the sender (no filter — receives
all frames). Re-subscribing from the same address updates the filter and
resets the failure counter.

### RX → client

Decoded frames are broadcast as `lora_frame` maps to subscribed clients.
Clients with a `sync_word` filter only receive frames matching their filter.

### Client → TX

Clients send `lora_tx` maps as UDP datagrams. `lora_trx` transmits the frame
and sends a `lora_tx_ack` back to the requesting client. TX requests also
register the sender if not already subscribed.

### PHY reconfiguration

Startup-only — edit `apps/config.toml` and restart `lora_trx`.  The
previous `lora_config` runtime API was removed when multi-chain RX
landed.

### lora-core daemon

`lora-core` (`scripts/src/lora/daemon/`) is the Python data-plane
daemon. It subscribes to one or more `lora_trx` upstreams on
`:5556`, performs diversity aggregation, runs the per-protocol
decoder chain (annotation pass), persists frames to DuckDB, and
re-broadcasts the annotated stream on `:5555`.

```bash
# Start the daemon (subscribes to upstreams in [core].upstream,
# serves consumers on the configured listen port — default :5555)
lora core

# Live frame viewer subscribing to lora-core (annotated stream).
lora mon

# Subscribe directly to raw frames from lora_trx (no aggregation,
# no decoder annotation, no `protocol` sub-map).
lora mon --connect 127.0.0.1:5556
```

Annotated stream from `lora-core :5555` carries the `protocol`
sub-map (when a decoder claims the sync_word) and the `source`
field. Raw stream from `lora_trx :5556` is the unannotated
`lora_frame` (no `protocol`, no `source`).

## Python examples

```python
# Subscribe to MeshCore/Reticulum frames only
import socket, cbor2

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sub = cbor2.dumps({"type": "subscribe", "sync_word": [0x12]})
sock.sendto(sub, ("127.0.0.1", 5555))
while True:
    data, addr = sock.recvfrom(65536)
    frame = cbor2.loads(data)
    if frame.get("type") == "lora_frame":
        phy = frame.get("phy", {})
        print(f"#{frame['seq']} {frame['payload_len']}B "
              f"CRC={'OK' if frame['crc_valid'] else 'FAIL'} "
              f"SNR={phy.get('snr_db', 0):.1f}dB")
```

```python
# Send TX request
import socket, cbor2

msg = cbor2.dumps({"type": "lora_tx", "payload": b"Hello"})
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(msg, ("127.0.0.1", 5555))
```

---

## Protocol annotation sub-map

`lora-core` runs the per-protocol decoder chain after diversity
aggregation. Successful decoders attach a top-level `protocol`
sub-map to the outgoing `lora_frame`. Decoder annotation runs **only**
in `lora-core`; the raw `lora_frame` from `lora_trx :5556` is
unannotated.

```cbor
{
  // ... all existing lora_frame fields unchanged ...
  "protocol": {
    "name":   "meshcore",       // text, decoder name (matches plugin registry key)
    "ok":     true,             // bool, decoder ran without error
    "fields": { ... }           // map, decoder-specific fields (sub-schema below)
  }
}
```

Three rules:

1. **Existing fields are never overwritten.** A decoder may not modify
   `payload`, `phy`, `carrier`, `crc_valid`, `seq`, etc.
2. **Failure leaves a record.** If a decoder claims the sync_word but
   parsing fails, the daemon emits
   `protocol = {name, ok: false, error: "<exception class>", fields: {}}`.
   That makes "frames whose decoder threw" queryable in DuckDB.
3. **Decoder-specific fields go under `fields`.** The top-level
   `protocol` keys are reserved (`name`, `ok`, `error`, `fields`,
   reserved for future expansion: `version`, `decode_ms`).

### `meshcore` (sync_word `0x12`)

```cbor
{
  "route":           "FLOOD",       // text, ROUTE_NAMES enum
  "payload_type":    "TXT",         // text, PAYLOAD_NAMES enum
  "version":         0,             // uint, header version
  "transport_codes": [0xab, 0xcd],  // uint array, optional, present when route is T_FLOOD/T_DIRECT
  "path":            h'AABB',       // bytes, optional, path hash list
  "path_len":        2,             // uint, hop count
  "sender_pubkey":   h'...',        // bytes, optional, present in ADVERT and ANON_REQ
  "sender_name":     "DO2THX",      // text, optional, decoded from ADVERT or learned-pubkey lookup
  "lat_e6":          53545130,      // int, optional, ADVERT location
  "lon_e6":          9948960,       // int, optional, ADVERT location
  "decrypted":       true,          // bool, payload decryption attempted and succeeded
  "decrypted_text":  "Hello",       // text, optional, present when decrypted=true and ptype is TXT/GRP_TXT
  "channel":         "public",      // text, optional, GRP_TXT decryption channel name
  "ack_hash":        h'12345678',   // bytes, optional, ACK target hash
  "msg_hash":        h'AABBCCDD'    // bytes, optional, deduplication hash
}
```

### `lorawan` (sync_word `0x34`)

```cbor
{
  "mtype":          "ConfDataUp",   // text, MType enum
  "is_uplink":      true,           // bool
  "is_data":        true,           // bool
  "is_join":        false,          // bool
  "dev_addr":       "01abcdef",     // text, hex 4-byte big-endian, lowercase, no prefix
  "fcnt":           42,             // uint
  "fctrl": {                        // map
    "adr":          true,
    "ack":          false,
    "adr_ack_req":  false,
    "fpending":     false,
    "fopts_len":    0
  },
  "fport":          1,              // uint, optional
  "mic":            h'AABBCCDD'     // bytes, 4-byte
}
```

### `meshtastic` (sync_word `0x2B`) — stub

```cbor
{
  "raw_payload": h'...'             // bytes; full protobuf parsing deferred
}
```

### `raw` (default, unknown sync_word)

```cbor
{
  "sync_word_hex": "0x77"           // text, observed sync word
}
```

**Consumers:** `lora.viz.mon` (renders `protocol.fields` per decoder),
`lora.storage.duckdb_writer` (persists `protocol.name` / `protocol.ok`
for query), `lora.daemon.fanout` (broadcasts annotated frames on
`:5555`).

---

## Multi-source

`lora-core` can subscribe to multiple `lora_trx` upstreams in parallel
(see `[[core.upstream]]` in `apps/config.toml`). Every annotated
`lora_frame` carries its originating source name verbatim.

### `source` field

Mandatory on output from `lora-core`. The string matches the
`[[core.upstream]].name` of the originating upstream. Single-source
configs still emit it (one named source).

```cbor
{
  // ... existing lora_frame fields ...
  "source": "radio_868"        // text, REQUIRED on lora-core output
}
```

### `subscribe.source` filter

Optional source-name allowlist. Empty or missing = receive frames from
all sources.

```cbor
{
  "type":      "subscribe",
  "sync_word": [18],           // optional, existing
  "source":    ["radio_868"]   // optional; empty/missing = all sources
}
```

### `lora_tx.source` selector

Optional source selector. If absent, `lora-core` routes the TX through
the first declared `[[core.upstream]]` and logs a one-time warning.

```cbor
{
  "type":    "lora_tx",
  "payload": h'...',
  "source":  "radio_868"       // optional
}
```

### `diversity.rx_sources`

`diversity` sub-map gains a per-candidate source list. The array index
matches `diversity.uuids` so consumers can pair "frame instance" with
"received-on source".

```cbor
"diversity": {
  // ... existing fields ...
  "rx_sources": ["radio_868", "radio_868"]   // text array, length == n_candidates
}
```

### `config.core.sources`

The `config` map sent to clients on subscribe lists every active
upstream so the consumer can populate UI dropdowns:

```cbor
"core": {
  // ... existing core sub-map fields ...
  "sources": [
    {"name": "radio_868", "host": "127.0.0.1", "port": 5556},
    {"name": "radio_2g4", "host": "127.0.0.1", "port": 5566}
  ]
}
```

**Consumers:** `lora.daemon.source` (sets `source` on every emitted
frame), `lora.daemon.tx_proxy` (resolves `lora_tx.source` to an
upstream UDP socket), `lora.viz.mon` (filters / colourises by source).

---

## Validation contract

`lora-core` runs the spec's five-layer validation chain on every
inbound CBOR datagram:

```
RF → lora_trx → UDP → [L1: socket] → [L2: CBOR parse] → [L3: schema validate]
                                                                │
                                                                ▼
                                       [L4: decoder validate + try/except]
                                                                │
                                                                ▼
                                                         [L5: storage]
```

| Layer | Mechanism |
|---|---|
| L1 — UDP socket | Max datagram 65536. Optional source-address bind: only accept from configured upstream when `[core].upstream_strict = true`. |
| L2 — CBOR parse | `cbor2.loads()` with `tag_hook=lambda *a: None`, `object_hook=None`. Wrapped in `try/except (CBORDecodeError, MemoryError, RecursionError)` → log + drop. Pre-parse size cap `MAX_FRAME_BYTES = 16 KiB`. |
| L3 — schema validate | `lora.core.schema.dispatch(d) -> Frame \| None`. Strict shape, type, length, range checks. Drops on violation. Never raises in the hot path. |
| L4 — decoder validate | Each decoder validates its own bytes. All decoder code under `try/except Exception`. Failure → `protocol = {ok: false, error: type(e).__name__}`. |
| L5 — storage | Parameterised SQL only. Bounded queue with oldest-drop policy. |

### L3 bounds (representative subset)

| Field | Rule |
|---|---|
| `type` | text, in known set; else drop silently |
| `payload` | bytes, ≤ 256 |
| `payload_len` | uint, ≤ 256, must equal `len(payload)` |
| `seq` | uint, < 2^32 |
| `phy.sf` | uint, 5 ≤ sf ≤ 12 |
| `phy.bw` | uint, in {7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000} |
| `phy.cr` | uint, 1 ≤ cr ≤ 4 |
| `phy.sync_word` | uint, 0..255 |
| `phy.snr_db`, `noise_floor_db`, `peak_db` | float, finite, magnitude ≤ 200 |
| `phy.cfo_int`, `cfo_frac`, `sfo_hat`, `ppm_error` | float, finite, magnitude bounded |
| `phy.channel_freq`, `frequency_corrected`, `sample_rate` | float, finite, > 0 |
| `payload_hash` | uint, < 2^64 |
| `id` | text, regex `^[0-9a-f-]{36}$` (UUIDv4 shape) |
| `ts` | text, ISO 8601 UTC; if missing or unparseable, daemon stamps its own |
| `device`, `decode_label` | text, ≤ 64 chars, printable |
| `bins` (spectrum) | bytes, length = `fft_size * 4`, `fft_size` ≤ 16384 |
| `subscribe.sync_word` | optional uint array, length ≤ 256, each 0..255 |
| `lora_tx.payload` | bytes, ≤ 255 |
| `lora_tx.repeat`, `gap_ms` | uint, sane caps (e.g. repeat ≤ 1024, gap ≤ 60000) |

Whitelist top-level keys — unknown keys logged at debug, stripped
before passing on. Reject duplicate map keys at parse time (custom
`cbor2` hook).

**Consumers:** `lora.core.schema` (L3 dispatcher),
`lora.daemon.source` (L1 + L2 wrapper), every plugin under
`lora.decoders.*` (L4 wrapper), `lora.storage.duckdb_writer` (L5
parameterised inserts).

