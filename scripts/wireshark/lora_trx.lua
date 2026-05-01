-- lora_trx.lua -- Wireshark dissector for lora_trx CBOR UDP frames
--
-- Decodes all CBOR message types on UDP port 5555 (lora_trx default):
-- lora_frame, lora_tx, lora_tx_ack, subscribe, config, status,
-- spectrum, spectrum_tx.
--
-- Install (auto-load, recommended):
--   cp scripts/lora_trx.lua ~/.local/lib/wireshark/plugins/
--   Then just open Wireshark -- the plugin loads automatically.
--   Verify: Help > About Wireshark > Plugins tab.
--
-- Load manually (tshark):
--   tshark -i lo0 -f "udp port 5555" -X lua_script:scripts/lora_trx.lua
--
-- Useful display filters:
--   lora_trx.type == "lora_frame"                          -- RX frames only
--   lora_trx.type != "spectrum" && lora_trx.type != "spectrum_tx"  -- hide waterfall
--   lora_trx.crc_valid == true                             -- CRC OK only
--   lora_trx.snr_db > 0                                   -- positive SNR only
--
-- CBOR parsing is minimal -- handles only the subset used by lora_trx
-- (maps with text keys, uint/bool/float64/bytes/array values, one nested map).

-- Guard against double-loading (auto-load from plugin dir + manual -X lua_script)
if lora_trx_proto_loaded then return end
lora_trx_proto_loaded = true

local lora = Proto("lora_trx", "LoRa TRX CBOR Frame")

-- Protocol fields -- RX frame
local f_type       = ProtoField.string("lora_trx.type",       "Type")
local f_ts         = ProtoField.string("lora_trx.ts",         "Timestamp")
local f_seq        = ProtoField.uint32("lora_trx.seq",        "Sequence",    base.DEC)
local f_sf         = ProtoField.uint8 ("lora_trx.sf",         "SF",          base.DEC)
local f_bw         = ProtoField.uint32("lora_trx.bw",         "Bandwidth",   base.DEC)
local f_cr         = ProtoField.uint8 ("lora_trx.cr",         "CR",          base.DEC)
local f_sync_word  = ProtoField.uint16("lora_trx.sync_word",  "Sync Word",   base.HEX)
local f_crc_valid  = ProtoField.bool  ("lora_trx.crc_valid",  "CRC Valid")
local f_snr_db     = ProtoField.double("lora_trx.snr_db",     "SNR (dB)")
local f_nf_db      = ProtoField.double("lora_trx.noise_floor","Noise Floor (dBFS)")
local f_peak_db    = ProtoField.double("lora_trx.peak_db",    "Peak (dBFS)")
local f_rx_channel = ProtoField.uint8 ("lora_trx.rx_channel", "RX Channel",  base.DEC)
local f_payload_len= ProtoField.uint16("lora_trx.payload_len","Payload Length",base.DEC)
local f_payload    = ProtoField.bytes ("lora_trx.payload",    "Payload")
local f_downchirp  = ProtoField.bool  ("lora_trx.is_downchirp","Downchirp")

-- TX request fields
local f_tx_ok       = ProtoField.bool  ("lora_trx.tx_ok",       "TX OK")
local f_dry_run     = ProtoField.bool  ("lora_trx.dry_run",     "Dry Run")
local f_repeat      = ProtoField.uint32("lora_trx.repeat",      "Repeat",        base.DEC)
local f_gap_ms      = ProtoField.uint32("lora_trx.gap_ms",      "Gap (ms)",      base.DEC)
local f_preamble_len= ProtoField.uint8 ("lora_trx.preamble_len","Preamble Length",base.DEC)

-- Subscribe fields
local f_sw_filter  = ProtoField.string("lora_trx.sw_filter",  "Sync Word Filter")

-- Config fields
local f_freq       = ProtoField.double("lora_trx.freq",       "Frequency (Hz)")
local f_rx_gain    = ProtoField.double("lora_trx.rx_gain",    "RX Gain (dB)")
local f_tx_gain    = ProtoField.double("lora_trx.tx_gain",    "TX Gain (dB)")
local f_preamble   = ProtoField.uint8 ("lora_trx.preamble",   "Preamble",      base.DEC)
local f_device     = ProtoField.string("lora_trx.device",     "Device")
local f_status_int = ProtoField.uint32("lora_trx.status_interval","Status Interval (s)",base.DEC)
local f_sample_rate= ProtoField.double("lora_trx.sample_rate","Sample Rate (S/s)")

-- Status fields
local f_frames_total   = ProtoField.uint32("lora_trx.frames_total",   "Total Frames",    base.DEC)
local f_frames_crc_ok  = ProtoField.uint32("lora_trx.frames_crc_ok",  "CRC OK Frames",   base.DEC)
local f_frames_crc_fail= ProtoField.uint32("lora_trx.frames_crc_fail","CRC Fail Frames",  base.DEC)

-- Spectrum fields
local f_fft_size    = ProtoField.uint32("lora_trx.fft_size",    "FFT Size",           base.DEC)
local f_center_freq = ProtoField.double("lora_trx.center_freq", "Center Frequency (Hz)")
local f_bins        = ProtoField.bytes ("lora_trx.bins",        "Magnitude Bins")

lora.fields = {
    f_type, f_ts, f_seq, f_sf, f_bw, f_cr, f_sync_word,
    f_crc_valid, f_snr_db, f_nf_db, f_peak_db, f_rx_channel,
    f_payload_len, f_payload, f_downchirp,
    f_tx_ok, f_dry_run, f_repeat, f_gap_ms, f_preamble_len,
    f_sw_filter,
    f_freq, f_rx_gain, f_tx_gain, f_preamble, f_device, f_status_int, f_sample_rate,
    f_frames_total, f_frames_crc_ok, f_frames_crc_fail,
    f_fft_size, f_center_freq, f_bins,
}

-- Minimal CBOR decoder (subset: uint, negint, bytes, text, array, map, float64, bool, null)
-- Returns (value, next_offset) or (nil, offset) on error

local function cbor_decode(buf, off)
    if off >= buf:len() then return nil, off end
    local initial = buf(off, 1):uint()
    local major = bit.rshift(initial, 5)
    local info  = bit.band(initial, 0x1f)
    off = off + 1

    -- Read argument value
    local val
    if info < 24 then
        val = info
    elseif info == 24 then
        val = buf(off, 1):uint(); off = off + 1
    elseif info == 25 then
        val = buf(off, 2):uint(); off = off + 2
    elseif info == 26 then
        val = buf(off, 4):uint(); off = off + 4
    elseif info == 27 then
        -- 64-bit: use float for large values (Lua 5.x limitation)
        val = buf(off, 4):uint() * 4294967296.0 + buf(off + 4, 4):uint()
        off = off + 8
    elseif info == 31 then
        val = nil  -- indefinite length (not used by lora_trx)
    else
        return nil, off
    end

    if major == 0 then
        -- Unsigned integer
        return val, off
    elseif major == 1 then
        -- Negative integer
        return -1 - val, off
    elseif major == 2 then
        -- Byte string
        if val == nil then return nil, off end
        local bytes_val = buf(off, val):bytes()
        return bytes_val, off + val
    elseif major == 3 then
        -- Text string
        if val == nil then return nil, off end
        local text = buf(off, val):string()
        return text, off + val
    elseif major == 4 then
        -- Array
        if val == nil then return nil, off end
        local arr = {}
        for i = 1, val do
            local item
            item, off = cbor_decode(buf, off)
            arr[i] = item
        end
        return arr, off
    elseif major == 5 then
        -- Map
        if val == nil then return nil, off end
        local map = {}
        for _ = 1, val do
            local k, v
            k, off = cbor_decode(buf, off)
            if k == nil then return nil, off end
            v, off = cbor_decode(buf, off)
            map[k] = v
        end
        return map, off
    elseif major == 7 then
        -- Simple values and floats
        if info == 20 then return false, off end  -- false
        if info == 21 then return true, off end   -- true
        if info == 22 then return nil, off end     -- null
        if info == 27 then
            -- float64 (already consumed 8 bytes for val)
            local float_val = buf(off - 8, 8):float()
            return float_val, off
        elseif info == 26 then
            -- float32
            local float_val = buf(off - 4, 4):float()
            return float_val, off
        end
        return val, off
    end

    return nil, off
end

-- Format a sync_word array as hex string: [0x12] or [0x12,0x2B]
local function format_sw_array(arr)
    if type(arr) ~= "table" then return "[]" end
    local parts = {}
    for _, v in ipairs(arr) do
        parts[#parts + 1] = string.format("0x%02X", v)
    end
    return "[" .. table.concat(parts, ",") .. "]"
end

-- Format frequency in MHz: 869618000.0 -> "869.618"
local function format_freq_mhz(hz)
    return string.format("%.3f", hz / 1e6)
end

-- Dissector function
function lora.dissector(tvbuf, pinfo, tree)
    if tvbuf:len() < 3 then return 0 end

    -- Try to decode the top-level CBOR map
    local ok, result = pcall(cbor_decode, tvbuf, 0)
    if not ok or type(result) ~= "table" then return 0 end

    local msg_type = result["type"]
    if msg_type == nil then return 0 end

    -- Spectrum / waterfall / decoder-telemetry messages arrive at 50+ Hz
    -- and would drown the packet list.  Return 0 so Wireshark treats
    -- them as generic Data and a display filter of `lora_trx` hides
    -- them entirely from LoRa views.  Use a Wireshark capture or
    -- display filter to include them if you want to inspect waterfall /
    -- per-decoder events.
    if msg_type == "spectrum" or msg_type == "spectrum_tx"
        or msg_type == "multisf_detect" or msg_type == "multisf_sync"
        or msg_type == "multisf_frame" then
        return 0
    end

    pinfo.cols.protocol = "LoRa"
    local subtree = tree:add(lora, tvbuf(), "LoRa TRX Frame")
    subtree:add(f_type, tvbuf(), msg_type)

    ---------- lora_frame (RX or TX) ----------
    if msg_type == "lora_frame" then
        local direction = result["direction"] or "rx"
        local dir_tag = (direction == "tx") and "TX" or "RX"
        pinfo.cols.info = string.format("%s #%s", dir_tag, tostring(result["seq"] or "?"))

        if result["ts"]  then subtree:add(f_ts, tvbuf(), result["ts"]) end
        if result["seq"] then subtree:add(f_seq, tvbuf(), result["seq"]) end

        -- Carrier subtree: frame identity (sync_word / sf / bw / cr).
        local carrier = result["carrier"]
        if type(carrier) == "table" then
            local car_tree = subtree:add(lora, tvbuf(), "Carrier")
            if carrier["sf"]        then car_tree:add(f_sf, tvbuf(), carrier["sf"]) end
            if carrier["bw"]        then car_tree:add(f_bw, tvbuf(), carrier["bw"]) end
            if carrier["cr"]        then car_tree:add(f_cr, tvbuf(), carrier["cr"]) end
            if carrier["sync_word"] then car_tree:add(f_sync_word, tvbuf(), carrier["sync_word"]) end
        end

        -- PHY subtree: DSP measurements (RX only — TX frames omit phy).
        local phy = result["phy"]
        if type(phy) == "table" then
            local phy_tree = subtree:add(lora, tvbuf(), "PHY")
            if phy["snr_db"]         then phy_tree:add(f_snr_db, tvbuf(), phy["snr_db"]) end
            if phy["noise_floor_db"] then phy_tree:add(f_nf_db, tvbuf(), phy["noise_floor_db"]) end
            if phy["peak_db"]        then phy_tree:add(f_peak_db, tvbuf(), phy["peak_db"]) end
        end

        local crc = result["crc_valid"]
        if crc ~= nil then subtree:add(f_crc_valid, tvbuf(), crc) end
        local crc_str = "?"
        if crc == true then crc_str = "OK"
        elseif crc == false then crc_str = "FAIL" end

        local sf  = (carrier and carrier["sf"])  or 0
        local snr = (phy and phy["snr_db"])      or 0
        pinfo.cols.info = string.format(
            "%s #%d %dB SF%d CRC_%s SNR=%.1f",
            dir_tag,
            result["seq"] or 0,
            result["payload_len"] or 0,
            sf, crc_str, snr
        )

        if result["rx_channel"]   then subtree:add(f_rx_channel, tvbuf(), result["rx_channel"]) end
        if result["payload_len"]  then subtree:add(f_payload_len, tvbuf(), result["payload_len"]) end
        if result["is_downchirp"] ~= nil then subtree:add(f_downchirp, tvbuf(), result["is_downchirp"]) end

        if result["payload"] then
            subtree:add(f_payload, tvbuf()):append_text(": " .. result["payload"]:tohex())
        end

    ---------- lora_tx (TX request) ----------
    elseif msg_type == "lora_tx" then
        local pay = result["payload"]
        local pay_len = 0
        if pay then pay_len = pay:len() end
        local cr_val = result["cr"]
        local sw_val = result["sync_word"]

        local info_parts = {string.format("TX %dB", pay_len)}
        if cr_val then info_parts[#info_parts + 1] = string.format("CR=%d", cr_val) end
        if sw_val then info_parts[#info_parts + 1] = string.format("SW=0x%02X", sw_val) end
        if result["repeat"] and result["repeat"] > 1 then
            info_parts[#info_parts + 1] = string.format("x%d", result["repeat"])
        end
        if result["dry_run"] then info_parts[#info_parts + 1] = "DRY" end
        pinfo.cols.info = table.concat(info_parts, " ")

        if cr_val  then subtree:add(f_cr, tvbuf(), cr_val) end
        if sw_val  then subtree:add(f_sync_word, tvbuf(), sw_val) end
        if result["preamble_len"] then subtree:add(f_preamble_len, tvbuf(), result["preamble_len"]) end
        if result["repeat"]  then subtree:add(f_repeat, tvbuf(), result["repeat"]) end
        if result["gap_ms"]  then subtree:add(f_gap_ms, tvbuf(), result["gap_ms"]) end
        if result["dry_run"] ~= nil then subtree:add(f_dry_run, tvbuf(), result["dry_run"]) end
        if pay_len > 0 then
            subtree:add(f_payload_len, tvbuf(), pay_len)
            subtree:add(f_payload, tvbuf()):append_text(": " .. pay:tohex())
        end

    ---------- lora_tx_ack ----------
    elseif msg_type == "lora_tx_ack" then
        local ok_val = result["ok"]
        local seq_val = result["seq"] or 0
        pinfo.cols.info = string.format("TX ACK #%d %s", seq_val, ok_val and "OK" or "FAIL")
        if result["seq"] then subtree:add(f_seq, tvbuf(), result["seq"]) end
        if ok_val ~= nil then subtree:add(f_tx_ok, tvbuf(), ok_val) end

    ---------- subscribe ----------
    elseif msg_type == "subscribe" then
        local sw = result["sync_word"]
        if sw then
            pinfo.cols.info = string.format("Subscribe SW=%s", format_sw_array(sw))
            subtree:add(f_sw_filter, tvbuf(), format_sw_array(sw))
        else
            pinfo.cols.info = "Subscribe (all)"
        end

    ---------- config ----------
    elseif msg_type == "config" then
        local phy = result["phy"]
        if type(phy) == "table" then
            pinfo.cols.info = string.format("Config SF%d BW=%d %sMHz",
                phy["sf"] or 0, phy["bw"] or 0,
                format_freq_mhz(phy["freq"] or 0))

            local phy_tree = subtree:add(lora, tvbuf(), "PHY")
            if phy["freq"]      then phy_tree:add(f_freq, tvbuf(), phy["freq"]) end
            if phy["sf"]        then phy_tree:add(f_sf, tvbuf(), phy["sf"]) end
            if phy["bw"]        then phy_tree:add(f_bw, tvbuf(), phy["bw"]) end
            if phy["cr"]        then phy_tree:add(f_cr, tvbuf(), phy["cr"]) end
            if phy["sync_word"] then phy_tree:add(f_sync_word, tvbuf(), phy["sync_word"]) end
            if phy["preamble"]  then phy_tree:add(f_preamble, tvbuf(), phy["preamble"]) end
            if phy["rx_gain"]   then phy_tree:add(f_rx_gain, tvbuf(), phy["rx_gain"]) end
            if phy["tx_gain"]   then phy_tree:add(f_tx_gain, tvbuf(), phy["tx_gain"]) end
        else
            pinfo.cols.info = "Config"
        end

        local srv = result["server"]
        if type(srv) == "table" then
            local srv_tree = subtree:add(lora, tvbuf(), "Server")
            if srv["device"]          then srv_tree:add(f_device, tvbuf(), srv["device"]) end
            if srv["status_interval"] then srv_tree:add(f_status_int, tvbuf(), srv["status_interval"]) end
            if srv["sample_rate"]     then srv_tree:add(f_sample_rate, tvbuf(), srv["sample_rate"]) end
        end

    ---------- status ----------
    elseif msg_type == "status" then
        local frames = result["frames"]
        if type(frames) == "table" then
            pinfo.cols.info = string.format("Status #%d OK=%d FAIL=%d",
                frames["total"] or 0, frames["crc_ok"] or 0, frames["crc_fail"] or 0)

            local fr_tree = subtree:add(lora, tvbuf(), "Frames")
            if frames["total"]    then fr_tree:add(f_frames_total, tvbuf(), frames["total"]) end
            if frames["crc_ok"]   then fr_tree:add(f_frames_crc_ok, tvbuf(), frames["crc_ok"]) end
            if frames["crc_fail"] then fr_tree:add(f_frames_crc_fail, tvbuf(), frames["crc_fail"]) end
        else
            pinfo.cols.info = "Status"
        end

        if result["ts"] then subtree:add(f_ts, tvbuf(), result["ts"]) end

        local phy = result["phy"]
        if type(phy) == "table" then
            local phy_tree = subtree:add(lora, tvbuf(), "PHY")
            if phy["rx_gain"] then phy_tree:add(f_rx_gain, tvbuf(), phy["rx_gain"]) end
            if phy["tx_gain"] then phy_tree:add(f_tx_gain, tvbuf(), phy["tx_gain"]) end
        end

    ---------- spectrum / spectrum_tx ----------
    elseif msg_type == "spectrum" or msg_type == "spectrum_tx" then
        local direction = (msg_type == "spectrum_tx") and "TX" or "RX"
        local fft_sz = result["fft_size"] or 0
        pinfo.cols.info = string.format("Spectrum %s %d bins %sMHz",
            direction, fft_sz, format_freq_mhz(result["center_freq"] or 0))

        if result["fft_size"]    then subtree:add(f_fft_size, tvbuf(), result["fft_size"]) end
        if result["center_freq"] then subtree:add(f_center_freq, tvbuf(), result["center_freq"]) end
        if result["sample_rate"] then subtree:add(f_sample_rate, tvbuf(), result["sample_rate"]) end
        if result["bins"] then
            subtree:add(f_bins, tvbuf()):append_text(
                string.format(": %d bytes", result["bins"]:len()))
        end

    ---------- unknown type ----------
    else
        pinfo.cols.info = string.format("Unknown: %s", tostring(msg_type))
    end

    return tvbuf:len()
end

-- Register for UDP port 5555
local udp_table = DissectorTable.get("udp.port")
udp_table:add(5555, lora)
