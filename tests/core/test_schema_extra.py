# SPDX-License-Identifier: ISC
"""Additional schema tests: untested validators, edge cases in dispatch."""

from __future__ import annotations

import math

import pytest

from lora.core.schema import (
    SchemaError,
    dispatch,
    validate_carrier,
    validate_diversity,
    validate_lora_frame,
    validate_lora_tx,
    validate_lora_tx_ack,
    validate_multisf_detect,
    validate_multisf_frame,
    validate_multisf_sync,
    validate_phy,
    validate_protocol_annotation,
    validate_spectrum,
    validate_status,
    validate_subscribe,
    validate_wideband_slot,
    validate_wideband_sweep,
)


def _phy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "sync_word": 0x12,
        "crc_valid": True,
    }
    base.update(overrides)
    return base


def _carrier(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sync_word": 0x12,
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "ldro_cfg": False,
    }
    base.update(overrides)
    return base


def _lora_frame(**overrides: object) -> dict[str, object]:
    payload = overrides.pop("payload", b"hello")
    base: dict[str, object] = {
        "type": "lora_frame",
        "ts": "2026-04-27T12:00:00.000Z",
        "seq": 1,
        "payload": payload,
        "payload_len": len(payload),  # type: ignore[arg-type]
        "crc_valid": True,
        "cr": 4,
        "is_downchirp": False,
        "payload_hash": 0x1234567890ABCDEF,
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "phy": _phy(),
        "carrier": _carrier(),
    }
    base.update(overrides)
    return base


# ---- lora_tx_ack ----------------------------------------------------------


def test_lora_tx_ack_ok() -> None:
    ack = validate_lora_tx_ack({"type": "lora_tx_ack", "seq": 42, "ok": True})
    assert ack.seq == 42
    assert ack.ok is True
    assert ack.error is None


def test_lora_tx_ack_fail_with_error() -> None:
    ack = validate_lora_tx_ack(
        {"type": "lora_tx_ack", "seq": 0, "ok": False, "error": "tx_timeout"}
    )
    assert ack.ok is False
    assert ack.error == "tx_timeout"


def test_lora_tx_ack_wrong_type() -> None:
    with pytest.raises(SchemaError, match="type must be 'lora_tx_ack'"):
        validate_lora_tx_ack({"type": "lora_frame", "seq": 0, "ok": True})


def test_lora_tx_ack_seq_out_of_range() -> None:
    with pytest.raises(SchemaError, match="seq"):
        validate_lora_tx_ack({"type": "lora_tx_ack", "seq": -1, "ok": True})


# ---- multisf_detect -------------------------------------------------------


def test_multisf_detect_round_trip() -> None:
    d = validate_multisf_detect(
        {"type": "multisf_detect", "ts": "2026-01-01T00:00:00Z", "sf": 7, "bin": 100}
    )
    assert d.type == "multisf_detect"
    assert d.sf == 7
    assert d.bin == 100


def test_multisf_detect_wrong_type() -> None:
    with pytest.raises(SchemaError):
        validate_multisf_detect({"type": "lora_frame", "ts": "", "sf": 7, "bin": 0})


def test_multisf_detect_sf_out_of_range() -> None:
    with pytest.raises(SchemaError, match="sf"):
        validate_multisf_detect({"type": "multisf_detect", "ts": "", "sf": 4, "bin": 0})


# ---- multisf_sync ---------------------------------------------------------


def test_multisf_sync_round_trip() -> None:
    s = validate_multisf_sync(
        {
            "type": "multisf_sync",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 10,
            "cfo_int": 5,
            "cfo_frac": 0.5,
            "snr_db": 6.0,
        }
    )
    assert s.sf == 10
    assert s.cfo_int == 5


def test_multisf_sync_cfo_int_negative() -> None:
    s = validate_multisf_sync(
        {
            "type": "multisf_sync",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 8,
            "cfo_int": -100,
            "cfo_frac": 0.0,
            "snr_db": 0.0,
        }
    )
    assert s.cfo_int == -100


# ---- multisf_frame --------------------------------------------------------


def test_multisf_frame_round_trip() -> None:
    f = validate_multisf_frame(
        {
            "type": "multisf_frame",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 8,
            "crc_ok": True,
            "len": 20,
            "cr": 1,
            "snr_db": 5.5,
        }
    )
    assert f.crc_ok is True
    assert f.len == 20


def test_multisf_frame_cr_zero_accepted() -> None:
    f = validate_multisf_frame(
        {
            "type": "multisf_frame",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 7,
            "crc_ok": False,
            "len": 0,
            "cr": 0,
            "snr_db": -5.0,
        }
    )
    assert f.cr == 0


# ---- wideband_sweep -------------------------------------------------------


def test_wideband_sweep_round_trip() -> None:
    w = validate_wideband_sweep(
        {
            "type": "wideband_sweep",
            "ts": "2026-01-01T00:00:00Z",
            "sweep": 5,
            "tainted": False,
            "overflows": 0,
            "zero_calls": 0,
            "total_calls": 100,
            "duration_ms": 500,
            "n_snapshots": 10,
            "n_hot": 2,
            "n_active": 3,
            "max_slots": 8,
        }
    )
    assert w.sweep == 5
    assert w.n_snapshots == 10
    assert w.tainted is False


# ---- wideband_slot --------------------------------------------------------


def test_wideband_slot_round_trip() -> None:
    s = validate_wideband_slot(
        {
            "type": "wideband_slot",
            "ts": "2026-01-01T00:00:00Z",
            "action": "activate",
            "slot": 0,
            "channel": 1,
            "freq": 869618000.0,
            "bw": 125000,
        }
    )
    assert s.action == "activate"
    assert s.channel == 1
    assert s.freq == 869618000.0


def test_wideband_slot_deactivate() -> None:
    s = validate_wideband_slot(
        {
            "type": "wideband_slot",
            "ts": "2026-01-01T00:00:00Z",
            "action": "deactivate",
            "slot": 3,
            "channel": 2,
            "freq": 868000000.0,
            "bw": 62500,
        }
    )
    assert s.action == "deactivate"


def test_wideband_slot_invalid_action() -> None:
    with pytest.raises(SchemaError, match="activate|deactivate"):
        validate_wideband_slot(
            {
                "type": "wideband_slot",
                "ts": "",
                "action": "toggle",
                "slot": 0,
                "channel": 0,
                "freq": 1,
                "bw": 62500,
            }
        )


def test_wideband_slot_unknown_bw() -> None:
    with pytest.raises(SchemaError, match="bw"):
        validate_wideband_slot(
            {
                "type": "wideband_slot",
                "ts": "",
                "action": "activate",
                "slot": 0,
                "channel": 0,
                "freq": 1,
                "bw": 99999,
            }
        )


# ---- diversity ------------------------------------------------------------


def test_diversity_minimal() -> None:
    d = validate_diversity(
        {
            "n_candidates": 0,
            "crc_mask": 0,
            "gap_us": 0,
            "rx_channels": [],
            "snr_db": [],
            "source_ids": [],
            "rx_sources": [],
        }
    )
    assert d.n_candidates == 0
    assert d.decoded_channel is None


def test_diversity_decoded_channel_present() -> None:
    d = validate_diversity(
        {
            "n_candidates": 2,
            "decoded_channel": 1,
            "crc_mask": 3,
            "gap_us": 100,
            "rx_channels": [0, 1],
            "snr_db": [3.0, 5.0],
            "source_ids": ["a", "b"],
            "rx_sources": ["r1", "r2"],
        }
    )
    assert d.decoded_channel == 1
    assert tuple(d.rx_sources) == ("r1", "r2")


def test_diversity_rx_channels_not_array() -> None:
    with pytest.raises(SchemaError, match="rx_channels"):
        validate_diversity(
            {
                "n_candidates": 1,
                "crc_mask": 0,
                "gap_us": 0,
                "rx_channels": "not-list",
                "snr_db": [],
                "source_ids": [],
                "rx_sources": [],
            }
        )


def test_diversity_snr_db_not_array() -> None:
    with pytest.raises(SchemaError, match="snr_db"):
        validate_diversity(
            {
                "n_candidates": 0,
                "crc_mask": 0,
                "gap_us": 0,
                "rx_channels": [],
                "snr_db": 42,
                "source_ids": [],
                "rx_sources": [],
            }
        )


def test_diversity_source_ids_not_array() -> None:
    with pytest.raises(SchemaError, match="source_ids"):
        validate_diversity(
            {
                "n_candidates": 0,
                "crc_mask": 0,
                "gap_us": 0,
                "rx_channels": [],
                "snr_db": [],
                "source_ids": None,
                "rx_sources": [],
            }
        )


def test_diversity_rx_sources_not_array() -> None:
    with pytest.raises(SchemaError, match="rx_sources"):
        validate_diversity(
            {
                "n_candidates": 0,
                "crc_mask": 0,
                "gap_us": 0,
                "rx_channels": [],
                "snr_db": [],
                "source_ids": [],
                "rx_sources": 123,
            }
        )


# ---- protocol_annotation --------------------------------------------------


def test_protocol_fields_must_be_map() -> None:
    with pytest.raises(SchemaError, match="fields"):
        validate_protocol_annotation(
            {"name": "test", "ok": True, "fields": "not-a-map"}
        )


# ---- subscribe ------------------------------------------------------------


def test_subscribe_sync_word_not_array() -> None:
    with pytest.raises(SchemaError, match="sync_word"):
        validate_subscribe({"type": "subscribe", "sync_word": 42})


def test_subscribe_source_not_array() -> None:
    with pytest.raises(SchemaError, match="source"):
        validate_subscribe({"type": "subscribe", "source": "single"})


def test_subscribe_source_entry_too_long() -> None:
    with pytest.raises(SchemaError, match="short string"):
        validate_subscribe({"type": "subscribe", "source": ["x" * 100]})


def test_subscribe_source_too_many() -> None:
    with pytest.raises(SchemaError, match="too long"):
        validate_subscribe(
            {"type": "subscribe", "source": [f"s{i}" for i in range(100)]}
        )


# ---- spectrum spectrum_tx type ---------------------------------------------


def test_spectrum_tx_type_accepted() -> None:
    bins = b"\x00" * (256 * 4)
    s = validate_spectrum(
        {
            "type": "spectrum_tx",
            "bins": bins,
            "fft_size": 256,
            "center_freq": 868000000.0,
            "sample_rate": 125000.0,
        }
    )
    assert s.type == "spectrum_tx"


# ---- carrier edge cases -----------------------------------------------------


def test_carrier_ldro_cfg_true() -> None:
    c = validate_carrier(_carrier(ldro_cfg=True))
    assert c.ldro_cfg is True


# ---- dispatch routing ------------------------------------------------------


def test_dispatch_routes_status() -> None:
    result = dispatch(
        {
            "type": "status",
            "ts": "2026-01-01T00:00:00Z",
            "phy": {"rx_gain": 40.0, "tx_gain": 70.0},
            "frames": {"total": 10, "crc_ok": 9, "crc_fail": 1},
            "rx_overflows": 0,
        }
    )
    assert result is not None
    assert result.type == "status"


def test_dispatch_routes_spectrum() -> None:
    bins = b"\x00" * (512 * 4)
    result = dispatch(
        {
            "type": "spectrum",
            "bins": bins,
            "fft_size": 512,
            "center_freq": 869618000.0,
            "sample_rate": 250000.0,
        }
    )
    assert result is not None
    assert result.type == "spectrum"


def test_dispatch_routes_spectrum_tx() -> None:
    bins = b"\x00" * (128 * 4)
    result = dispatch(
        {
            "type": "spectrum_tx",
            "bins": bins,
            "fft_size": 128,
            "center_freq": 868000000.0,
            "sample_rate": 125000.0,
        }
    )
    assert result is not None


def test_dispatch_routes_lora_tx_ack() -> None:
    result = dispatch({"type": "lora_tx_ack", "seq": 0, "ok": True})
    assert result is not None
    assert result.type == "lora_tx_ack"


def test_dispatch_routes_multisf_detect() -> None:
    result = dispatch(
        {"type": "multisf_detect", "ts": "2026-01-01T00:00:00Z", "sf": 8, "bin": 0}
    )
    assert result is not None


def test_dispatch_routes_multisf_sync() -> None:
    result = dispatch(
        {
            "type": "multisf_sync",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 8,
            "cfo_int": 0,
            "cfo_frac": 0.0,
            "snr_db": 0.0,
        }
    )
    assert result is not None


def test_dispatch_routes_multisf_frame() -> None:
    result = dispatch(
        {
            "type": "multisf_frame",
            "ts": "2026-01-01T00:00:00Z",
            "sf": 8,
            "crc_ok": True,
            "len": 5,
            "cr": 4,
            "snr_db": 0.0,
        }
    )
    assert result is not None


def test_dispatch_routes_wideband_sweep() -> None:
    result = dispatch(
        {
            "type": "wideband_sweep",
            "ts": "2026-01-01T00:00:00Z",
            "sweep": 0,
            "tainted": False,
            "overflows": 0,
            "zero_calls": 0,
            "total_calls": 0,
            "duration_ms": 0,
            "n_snapshots": 0,
            "n_hot": 0,
            "n_active": 0,
            "max_slots": 0,
        }
    )
    assert result is not None


def test_dispatch_routes_wideband_slot() -> None:
    result = dispatch(
        {
            "type": "wideband_slot",
            "ts": "2026-01-01T00:00:00Z",
            "action": "activate",
            "slot": 0,
            "channel": 0,
            "freq": 868000000.0,
            "bw": 62500,
        }
    )
    assert result is not None


# ---- lora_frame direction / rx_channel / device / decode_label ------------


def test_lora_frame_direction_tx() -> None:
    f = validate_lora_frame(_lora_frame(direction="tx"))
    assert f.direction == "tx"


def test_lora_frame_direction_invalid() -> None:
    with pytest.raises(SchemaError, match="direction"):
        validate_lora_frame(_lora_frame(direction="up"))


def test_lora_frame_rx_channel() -> None:
    f = validate_lora_frame(_lora_frame(rx_channel=3))
    assert f.rx_channel == 3


def test_lora_frame_device() -> None:
    f = validate_lora_frame(_lora_frame(device="b210"))
    assert f.device == "b210"


def test_lora_frame_decode_label() -> None:
    f = validate_lora_frame(_lora_frame(decode_label="sf8_125k"))
    assert f.decode_label == "sf8_125k"


# ---- phy optional fields all None -----------------------------------------


def test_phy_optional_fields_default_none() -> None:
    p = validate_phy(_phy())
    assert p.snr_db is None
    assert p.ldro_detected is None
    assert p.ldro_n_samples is None
    assert p.ldro_match is None


# ---- check_float with int value -------------------------------------------


def test_phy_accepts_int_for_float_field() -> None:
    p = validate_phy(_phy(snr_db=5))
    assert p.snr_db == 5.0


# ---- non-finite float in optional float field ------------------------------


def test_phy_rejects_nan_in_optional_float() -> None:
    with pytest.raises(SchemaError, match="finite"):
        validate_phy(_phy(noise_floor_db=float("nan")))


def test_phy_rejects_inf_in_optional_float() -> None:
    with pytest.raises(SchemaError, match="finite"):
        validate_phy(_phy(peak_db=math.inf))


# ---- lora_tx edge cases ----------------------------------------------------


def test_lora_tx_with_source() -> None:
    t = validate_lora_tx({"type": "lora_tx", "payload": b"data", "source": "radio_868"})
    assert t.source == "radio_868"


def test_lora_tx_missing_payload_required() -> None:
    with pytest.raises(SchemaError, match="payload"):
        validate_lora_tx({"type": "lora_tx"})


# ---- status edge cases -----------------------------------------------------


def test_status_phy_must_be_map() -> None:
    with pytest.raises(SchemaError, match="status.phy"):
        validate_status(
            {
                "type": "status",
                "ts": "",
                "phy": "not-a-map",
                "frames": {"total": 0, "crc_ok": 0, "crc_fail": 0},
                "rx_overflows": 0,
            }
        )


def test_status_frames_must_be_map() -> None:
    with pytest.raises(SchemaError, match="status.frames"):
        validate_status(
            {
                "type": "status",
                "ts": "",
                "phy": {"rx_gain": 0, "tx_gain": 0},
                "frames": None,
                "rx_overflows": 0,
            }
        )
