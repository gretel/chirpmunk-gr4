# SPDX-License-Identifier: ISC
"""Table-driven validation tests for lora.core.schema.

Covers:

  - happy path round-trip per top-level type;
  - bound violations (sf out of range, bw not in KNOWN_BWS, payload_len
    mismatch, oversized text fields, etc.) that must raise SchemaError
    when called via the typed validators;
  - silent drop semantics: dispatch() returns None on the same inputs
    rather than raising.
"""

from __future__ import annotations

import pytest

from lora.core.schema import (
    KNOWN_BWS,
    SchemaError,
    dispatch,
    validate_carrier,
    validate_lora_frame,
    validate_lora_tx,
    validate_phy,
    validate_protocol_annotation,
    validate_spectrum,
    validate_status,
    validate_subscribe,
)

# ---- fixtures ------------------------------------------------------------


def _phy(**overrides):
    base = {
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "sync_word": 0x12,
        "crc_valid": True,
    }
    base.update(overrides)
    return base


def _carrier(**overrides):
    base = {
        "sync_word": 0x12,
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "ldro_cfg": False,
    }
    base.update(overrides)
    return base


def _lora_frame(**overrides):
    payload = overrides.pop("payload", b"hello")
    base = {
        "type": "lora_frame",
        "ts": "2026-04-27T12:00:00.000Z",
        "seq": 1,
        "payload": payload,
        "payload_len": len(payload),
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


# ---- phy -----------------------------------------------------------------


def test_phy_accepts_valid():
    p = validate_phy(_phy())
    assert p.sf == 8
    assert p.bw == 62500
    assert p.crc_valid is True


@pytest.mark.parametrize(
    "field, value, msg",
    [
        ("sf", 4, "sf"),
        ("sf", 13, "sf"),
        ("bw", 99999, "bw"),
        ("cr", 0, "cr"),
        ("cr", 5, "cr"),
        ("sync_word", -1, "sync_word"),
        ("sync_word", 256, "sync_word"),
    ],
)
def test_phy_rejects_out_of_bounds(field, value, msg):
    with pytest.raises(SchemaError, match=msg):
        validate_phy(_phy(**{field: value}))


def test_phy_rejects_non_finite_floats():
    with pytest.raises(SchemaError, match="finite"):
        validate_phy(_phy(snr_db=float("nan")))


def test_phy_optional_fields_round_trip():
    p = validate_phy(_phy(snr_db=12.5, ldro_detected=True, ldro_n_samples=20))
    assert p.snr_db == 12.5
    assert p.ldro_detected is True
    assert p.ldro_n_samples == 20


# ---- carrier -------------------------------------------------------------


def test_carrier_accepts_valid():
    c = validate_carrier(_carrier())
    assert c.sync_word == 0x12


def test_carrier_rejects_unknown_bw():
    with pytest.raises(SchemaError):
        validate_carrier(_carrier(bw=99_999))


# ---- lora_frame ----------------------------------------------------------


def test_lora_frame_round_trip():
    f = validate_lora_frame(_lora_frame())
    assert f.type == "lora_frame"
    assert f.payload == b"hello"
    assert f.phy.sync_word == 0x12
    assert f.diversity is None
    assert f.protocol is None


def test_lora_frame_with_protocol_annotation():
    f = validate_lora_frame(
        _lora_frame(
            protocol={
                "name": "meshcore",
                "ok": True,
                "fields": {"route": "FLOOD", "payload_type": "TXT"},
            }
        )
    )
    assert f.protocol is not None
    assert f.protocol.name == "meshcore"
    assert f.protocol.ok is True
    assert f.protocol.fields["route"] == "FLOOD"


def test_lora_frame_with_diversity():
    f = validate_lora_frame(
        _lora_frame(
            diversity={
                "n_candidates": 2,
                "decoded_channel": 1,
                "rx_channels": [0, 1],
                "snr_db": [3.2, 5.1],
                "crc_mask": 3,
                "gap_us": 20000,
                "source_ids": ["uuid1", "uuid2"],
                "rx_sources": ["radio_868", "radio_868"],
            }
        )
    )
    assert f.diversity is not None
    assert f.diversity.n_candidates == 2
    assert tuple(f.diversity.rx_sources) == ("radio_868", "radio_868")


def test_lora_frame_payload_len_mismatch():
    with pytest.raises(SchemaError, match="payload_len"):
        validate_lora_frame(_lora_frame(payload=b"hello", payload_len=99))


def test_lora_frame_payload_too_large():
    with pytest.raises(SchemaError, match="exceeds"):
        validate_lora_frame(_lora_frame(payload=b"x" * 257, payload_len=257))


def test_lora_frame_rejects_bad_uuid():
    with pytest.raises(SchemaError, match="UUID"):
        validate_lora_frame(_lora_frame(id="not-a-uuid"))


def test_lora_frame_rejects_wrong_type():
    with pytest.raises(SchemaError, match="lora_frame"):
        validate_lora_frame(_lora_frame(type="other"))


def test_lora_frame_with_source():
    f = validate_lora_frame(_lora_frame(source="radio_868"))
    assert f.source == "radio_868"


def test_lora_frame_default_source_empty():
    f = validate_lora_frame(_lora_frame())
    # ``source`` is optional on input (raw lora_trx :5556 doesn't set it).
    assert f.source == ""


def test_lora_frame_top_level_cr_overlays_from_carrier():
    """C++ FrameSink emits ``cr`` inside ``carrier`` only — top-level
    mirror must be optional with fallback to ``carrier.cr``. Regression
    for the bridge contact-learning silent drop: every lora_frame from
    lora_trx was discarded at L3 because the top-level cr field is
    absent in the producer's CBOR layout (FrameSink.hpp:332-345).
    """
    frame = _lora_frame()
    del frame["cr"]
    # carrier.cr is preserved by _carrier()
    f = validate_lora_frame(frame)
    assert f.cr == 4
    assert f.carrier.cr == 4
    # dispatch must succeed (not silently drop) — this is the actual
    # production path the bridge depends on.
    assert dispatch(frame) is not None


def test_lora_frame_missing_cr_anywhere_rejected():
    """If neither top-level nor carrier.cr is present, validation must
    still fail loudly — the overlay is a producer-shape accommodation,
    not a permissive default."""
    carrier_no_cr = {k: v for k, v in _carrier().items() if k != "cr"}
    frame = _lora_frame(carrier=carrier_no_cr)
    del frame["cr"]
    with pytest.raises(SchemaError, match="cr"):
        validate_lora_frame(frame)


def test_lora_frame_phy_falls_back_to_carrier_for_identity_fields():
    """Real lora_trx puts sf/bw/cr/sync_word in `carrier` only and DSP state
    in `phy`. The validator must still produce a complete Phy by overlaying
    carrier's identity fields when phy lacks them, and pull crc_valid from
    the top-level mirror."""
    frame = _lora_frame()
    # Strip the duplicated identity fields from phy — DSP-only payload, like
    # what lora_trx actually emits.
    frame["phy"] = {"snr_db": 12.5, "noise_floor_db": -42.0}
    # Top-level crc_valid mirrors phy.crc_valid in the real wire format.
    frame["crc_valid"] = True
    f = validate_lora_frame(frame)
    assert f.phy.sf == 8
    assert f.phy.bw == 62500
    assert f.phy.cr == 4
    assert f.phy.sync_word == 0x12
    assert f.phy.crc_valid is True
    assert f.phy.snr_db == 12.5


def test_lora_frame_phy_keeps_explicit_phy_fields_over_carrier():
    """When phy DOES have the identity fields, they win over carrier's copy
    (no overlay)."""
    frame = _lora_frame()
    frame["phy"] = {
        "sf": 9,
        "bw": 125000,
        "cr": 4,
        "sync_word": 0x34,
        "crc_valid": False,
    }
    # carrier still has sf=8, bw=62500, sync_word=0x12.
    f = validate_lora_frame(frame)
    assert f.phy.sf == 9  # phy wins
    assert f.phy.bw == 125000
    assert f.phy.sync_word == 0x34
    assert f.phy.crc_valid is False


# ---- protocol annotation -------------------------------------------------


def test_protocol_annotation_failure_record():
    p = validate_protocol_annotation(
        {"name": "meshcore", "ok": False, "error": "bad_header", "fields": {}}
    )
    assert p.ok is False
    assert p.error == "bad_header"


# ---- subscribe -----------------------------------------------------------


def test_subscribe_no_filter():
    s = validate_subscribe({"type": "subscribe"})
    assert s.sync_word is None
    assert s.source is None


def test_subscribe_with_sync_word_filter():
    s = validate_subscribe({"type": "subscribe", "sync_word": [0x12, 0x34]})
    assert s.sync_word == (0x12, 0x34)


def test_subscribe_with_source_filter():
    s = validate_subscribe({"type": "subscribe", "source": ["radio_868"]})
    assert s.source == ("radio_868",)


def test_subscribe_rejects_oversized_sync_word_array():
    with pytest.raises(SchemaError, match="too long"):
        validate_subscribe({"type": "subscribe", "sync_word": list(range(257))})


def test_subscribe_rejects_out_of_range_sync_word():
    with pytest.raises(SchemaError, match="out of range"):
        validate_subscribe({"type": "subscribe", "sync_word": [256]})


# ---- lora_tx -------------------------------------------------------------


def test_lora_tx_minimal():
    t = validate_lora_tx({"type": "lora_tx", "payload": b"hi"})
    assert t.payload == b"hi"
    assert t.repeat is None
    assert t.source is None


def test_lora_tx_full():
    t = validate_lora_tx(
        {
            "type": "lora_tx",
            "payload": b"hi",
            "seq": 5,
            "cr": 4,
            "sync_word": 0x12,
            "preamble_len": 8,
            "repeat": 3,
            "gap_ms": 1000,
            "dry_run": False,
            "source": "radio_868",
        }
    )
    assert t.repeat == 3
    assert t.source == "radio_868"


def test_lora_tx_payload_too_large():
    with pytest.raises(SchemaError, match="exceeds"):
        validate_lora_tx({"type": "lora_tx", "payload": b"x" * 256})


def test_lora_tx_repeat_too_large():
    with pytest.raises(SchemaError, match="repeat"):
        validate_lora_tx({"type": "lora_tx", "payload": b"hi", "repeat": 99999})


# ---- status / spectrum ---------------------------------------------------


def test_status_round_trip():
    s = validate_status(
        {
            "type": "status",
            "ts": "2026-04-27T12:00:00.000Z",
            "phy": {"rx_gain": 40.0, "tx_gain": 73.5},
            "frames": {"total": 100, "crc_ok": 95, "crc_fail": 5},
            "rx_overflows": 0,
        }
    )
    assert s.frames.total == 100


def test_spectrum_round_trip():
    bins = b"\x00" * (1024 * 4)
    s = validate_spectrum(
        {
            "type": "spectrum",
            "bins": bins,
            "fft_size": 1024,
            "center_freq": 869618000.0,
            "sample_rate": 250000.0,
        }
    )
    assert s.fft_size == 1024
    assert len(s.bins) == 4096


def test_spectrum_bins_size_mismatch():
    with pytest.raises(SchemaError, match="bins"):
        validate_spectrum(
            {
                "type": "spectrum",
                "bins": b"\x00" * 100,
                "fft_size": 1024,
                "center_freq": 869618000.0,
                "sample_rate": 250000.0,
            }
        )


def test_spectrum_fft_size_too_large():
    with pytest.raises(SchemaError):
        validate_spectrum(
            {
                "type": "spectrum",
                "bins": b"",
                "fft_size": 1_000_000,
                "center_freq": 869618000.0,
                "sample_rate": 250000.0,
            }
        )


# ---- dispatch ------------------------------------------------------------


def test_dispatch_routes_lora_frame():
    f = dispatch(_lora_frame())
    assert f is not None
    assert f.type == "lora_frame"


def test_dispatch_returns_none_on_unknown_type():
    assert dispatch({"type": "nope"}) is None


def test_dispatch_returns_none_on_missing_type():
    assert dispatch({}) is None


def test_dispatch_returns_none_on_non_mapping():
    assert dispatch("string") is None  # type: ignore[arg-type]
    assert dispatch(None) is None  # type: ignore[arg-type]


def test_dispatch_silent_on_validation_failure():
    # payload_len mismatch raises in validate_lora_frame; dispatch returns None.
    bad = _lora_frame(payload=b"hi", payload_len=99)
    assert dispatch(bad) is None


def test_dispatch_routes_subscribe():
    s = dispatch({"type": "subscribe"})
    assert s is not None
    assert s.type == "subscribe"


def test_dispatch_routes_lora_tx():
    t = dispatch({"type": "lora_tx", "payload": b"x"})
    assert t is not None


# ---- bandwidth set -------------------------------------------------------


def test_known_bws_match_spec():
    """Spec L3 bounds: bw must be in {7800, 10400, ..., 500000}."""
    expected = {
        7800,
        10400,
        15600,
        20800,
        31250,
        41700,
        62500,
        125000,
        250000,
        500000,
    }
    assert KNOWN_BWS == expected
