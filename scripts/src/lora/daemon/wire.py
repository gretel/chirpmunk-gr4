#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""CBOR wire-shape encoders for typed events on the daemon's broadcast bus.

These helpers re-encode :mod:`lora.core.types` dataclasses back to the
legacy dict shape consumed by ``lora_mon`` / ``meshcore_bridge.py`` /
the rest of the existing toolchain. The receive path (CBOR ->
typed) lives in :mod:`lora.core.schema`; this module is the dual.

Optional fields are included only when their typed value is not
``None``, mirroring the producer side in ``lora_trx``.
"""

from __future__ import annotations

from typing import Any

from lora.core.types import (
    Carrier,
    Diversity,
    LoraFrame,
    LoraTxAck,
    Phy,
    ProtocolAnnotation,
    Spectrum,
    Status,
)

_PHY_OPTIONAL_FIELDS: tuple[str, ...] = (
    "snr_db",
    "noise_floor_db",
    "peak_db",
    "snr_db_td",
    "channel_freq",
    "decode_bw",
    "cfo_int",
    "cfo_frac",
    "sfo_hat",
    "sample_rate",
    "frequency_corrected",
    "ppm_error",
    "ldro_detected",
    "ldro_match",
    "ldro_lsb0_fraction",
    "ldro_n_samples",
)


def _phy_to_dict(phy: Phy) -> dict[str, Any]:
    d: dict[str, Any] = {
        "sf": phy.sf,
        "bw": phy.bw,
        "cr": phy.cr,
        "sync_word": phy.sync_word,
        "crc_valid": phy.crc_valid,
    }
    for attr in _PHY_OPTIONAL_FIELDS:
        v = getattr(phy, attr)
        if v is not None:
            d[attr] = v
    return d


def _carrier_to_dict(c: Carrier) -> dict[str, Any]:
    return {
        "sync_word": c.sync_word,
        "sf": c.sf,
        "bw": c.bw,
        "cr": c.cr,
        "ldro_cfg": c.ldro_cfg,
    }


def _diversity_to_dict(d: Diversity) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n_candidates": d.n_candidates,
        "rx_channels": list(d.rx_channels),
        "snr_db": list(d.snr_db),
        "crc_mask": d.crc_mask,
        "gap_us": d.gap_us,
        "source_ids": list(d.source_ids),
        "rx_sources": list(d.rx_sources),
    }
    if d.decoded_channel is not None:
        out["decoded_channel"] = d.decoded_channel
    return out


def _protocol_to_dict(p: ProtocolAnnotation) -> dict[str, Any]:
    out: dict[str, Any] = {"name": p.name, "ok": p.ok, "fields": dict(p.fields)}
    if p.error is not None:
        out["error"] = p.error
    return out


def frame_to_cbor_dict(frame: LoraFrame) -> dict[str, Any]:
    """Re-encode a typed :class:`LoraFrame` to its legacy wire-shape dict."""
    d: dict[str, Any] = {
        "type": "lora_frame",
        "ts": frame.ts,
        "seq": frame.seq,
        "payload": frame.payload,
        "payload_len": frame.payload_len,
        "crc_valid": frame.crc_valid,
        "cr": frame.cr,
        "is_downchirp": frame.is_downchirp,
        "payload_hash": frame.payload_hash,
        "id": frame.id,
        "phy": _phy_to_dict(frame.phy),
        "carrier": _carrier_to_dict(frame.carrier),
        "source": frame.source,
        "direction": frame.direction,
    }
    for attr in ("rx_channel", "decode_label", "device"):
        v = getattr(frame, attr)
        if v is not None:
            d[attr] = v
    if frame.diversity is not None:
        d["diversity"] = _diversity_to_dict(frame.diversity)
    if frame.protocol is not None:
        d["protocol"] = _protocol_to_dict(frame.protocol)
    return d


def status_to_cbor_dict(s: Status) -> dict[str, Any]:
    return {
        "type": "status",
        "ts": s.ts,
        "phy": {"rx_gain": s.phy.rx_gain, "tx_gain": s.phy.tx_gain},
        "frames": {
            "total": s.frames.total,
            "crc_ok": s.frames.crc_ok,
            "crc_fail": s.frames.crc_fail,
        },
        "rx_overflows": s.rx_overflows,
    }


def spectrum_to_cbor_dict(s: Spectrum) -> dict[str, Any]:
    return {
        "type": s.type,
        "bins": s.bins,
        "fft_size": s.fft_size,
        "center_freq": s.center_freq,
        "sample_rate": s.sample_rate,
    }


def lora_tx_ack_to_cbor_dict(a: LoraTxAck) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "lora_tx_ack", "seq": a.seq, "ok": a.ok}
    if a.error is not None:
        out["error"] = a.error
    return out
