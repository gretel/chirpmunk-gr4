#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""CBOR input validation for the lora-core data plane.

Strategy
--------

This is the L3 layer of the always-on five-layer validation pipeline
documented in spec Section 3. Inputs arrive as CBOR-decoded plain
dicts; this module:

- enforces the type, shape, and bound rules from the spec's L3 table;
- never raises into the daemon hot path (callers use :func:`dispatch`,
  which returns ``None`` on any rejection and logs at debug);
- raises :class:`SchemaError` only when called directly from tests so
  the failing case can be asserted.

The producers of ``Phy`` / ``Carrier`` / ``LoraFrame`` / etc. are the
typed dataclasses in :mod:`lora.core.types`. This module exists
specifically to bridge the untrusted dict ↔ trusted dataclass
boundary.

What's *not* validated here
---------------------------

- decoder-specific payload bytes (covered by L4 in each decoder)
- crypto signatures (decoder responsibility)
- inter-frame consistency (e.g. seq monotonicity — daemon concern, not
  schema concern)

Everything in this module is deliberately small enough to read in one
sitting; complexity belongs in the typed dataclass definitions or the
decoders.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from .types import (
    Carrier,
    Diversity,
    LoraFrame,
    LoraTx,
    LoraTxAck,
    MultisfDetect,
    MultisfFrame,
    MultisfSync,
    Phy,
    ProtocolAnnotation,
    Spectrum,
    Status,
    StatusFrames,
    StatusPhy,
    Subscribe,
    WidebandSlot,
    WidebandSweep,
)

# ---- bounds --------------------------------------------------------------

MAX_PAYLOAD = 256
MAX_BINS_FFT_SIZE = 16384
MAX_PATH_LEN = 64
MAX_TEXT_FIELD = 64

KNOWN_BWS: frozenset[int] = frozenset(
    {7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000}
)

KNOWN_TYPES: frozenset[str] = frozenset(
    {
        "lora_frame",
        "status",
        "spectrum",
        "spectrum_tx",
        "subscribe",
        "lora_tx",
        "lora_tx_ack",
        "config",
        "multisf_detect",
        "multisf_sync",
        "multisf_frame",
        "wideband_sweep",
        "wideband_slot",
    }
)

_UUID_RE = re.compile(r"^[0-9a-f-]{36}$")


# ---- exceptions ----------------------------------------------------------


class SchemaError(ValueError):
    """Raised by direct ``validate_*`` calls when input violates the schema.

    Never propagates out of :func:`dispatch` — the dispatcher catches
    it and returns ``None``.
    """


# ---- primitive helpers ---------------------------------------------------


def _check_uint(
    d: Mapping[str, Any],
    key: str,
    *,
    lo: int = 0,
    hi: int = 2**32,
    required: bool = True,
    default: int | None = None,
) -> int | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required uint field {key!r}")
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise SchemaError(f"{key} must be uint, got {type(v).__name__}")
    if v < lo or v > hi:
        raise SchemaError(f"{key}={v} out of [{lo}, {hi}]")
    return v


def _check_int(
    d: Mapping[str, Any],
    key: str,
    *,
    lo: int = -(2**31),
    hi: int = 2**31 - 1,
    required: bool = False,
) -> int | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required int field {key!r}")
        return None
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise SchemaError(f"{key} must be int, got {type(v).__name__}")
    if v < lo or v > hi:
        raise SchemaError(f"{key}={v} out of [{lo}, {hi}]")
    return v


def _check_float(
    d: Mapping[str, Any],
    key: str,
    *,
    lo: float = -math.inf,
    hi: float = math.inf,
    required: bool = False,
    finite: bool = True,
) -> float | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required float field {key!r}")
        return None
    v = d[key]
    # int is acceptable where float is expected.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SchemaError(f"{key} must be float, got {type(v).__name__}")
    fv = float(v)
    if finite and not math.isfinite(fv):
        raise SchemaError(f"{key} must be finite, got {fv}")
    if fv < lo or fv > hi:
        raise SchemaError(f"{key}={fv} out of [{lo}, {hi}]")
    return fv


def _check_bool(
    d: Mapping[str, Any],
    key: str,
    *,
    required: bool = True,
    default: bool | None = None,
) -> bool | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required bool field {key!r}")
        return default
    v = d[key]
    if not isinstance(v, bool):
        raise SchemaError(f"{key} must be bool, got {type(v).__name__}")
    return v


def _check_text(
    d: Mapping[str, Any],
    key: str,
    *,
    maxlen: int = MAX_TEXT_FIELD,
    required: bool = True,
    default: str | None = None,
) -> str | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required text field {key!r}")
        return default
    v = d[key]
    if not isinstance(v, str):
        raise SchemaError(f"{key} must be str, got {type(v).__name__}")
    if len(v) > maxlen:
        raise SchemaError(f"{key} exceeds {maxlen} chars (got {len(v)})")
    return v


def _check_bytes(
    d: Mapping[str, Any],
    key: str,
    *,
    maxlen: int,
    required: bool = True,
) -> bytes | None:
    if key not in d:
        if required:
            raise SchemaError(f"missing required bytes field {key!r}")
        return None
    v = d[key]
    if not isinstance(v, (bytes, bytearray, memoryview)):
        raise SchemaError(f"{key} must be bytes, got {type(v).__name__}")
    raw = bytes(v)
    if len(raw) > maxlen:
        raise SchemaError(f"{key} exceeds {maxlen} bytes (got {len(raw)})")
    return raw


# ---- per-type validators -------------------------------------------------


def validate_phy(d: Mapping[str, Any]) -> Phy:
    sf = _check_uint(d, "sf", lo=5, hi=12)
    bw = _check_uint(d, "bw", lo=0, hi=2_000_000)
    if bw not in KNOWN_BWS:
        raise SchemaError(f"phy.bw={bw} not in known bandwidths {sorted(KNOWN_BWS)}")
    cr = _check_uint(d, "cr", lo=1, hi=4)
    sw = _check_uint(d, "sync_word", lo=0, hi=255)
    crc_valid = _check_bool(d, "crc_valid")
    assert sf is not None and bw is not None and cr is not None
    assert sw is not None and crc_valid is not None
    return Phy(
        sf=sf,
        bw=bw,
        cr=cr,
        sync_word=sw,
        crc_valid=crc_valid,
        snr_db=_check_float(d, "snr_db", lo=-200, hi=200),
        noise_floor_db=_check_float(d, "noise_floor_db", lo=-200, hi=200),
        peak_db=_check_float(d, "peak_db", lo=-200, hi=200),
        snr_db_td=_check_float(d, "snr_db_td", lo=-200, hi=200),
        channel_freq=_check_float(d, "channel_freq", lo=0),
        decode_bw=_check_float(d, "decode_bw", lo=0),
        cfo_int=_check_float(d, "cfo_int", lo=-1e6, hi=1e6),
        cfo_frac=_check_float(d, "cfo_frac", lo=-1e3, hi=1e3),
        sfo_hat=_check_float(d, "sfo_hat", lo=-1, hi=1),
        sample_rate=_check_float(d, "sample_rate", lo=0),
        frequency_corrected=_check_float(d, "frequency_corrected", lo=0),
        ppm_error=_check_float(d, "ppm_error", lo=-1e6, hi=1e6),
        ldro_detected=_check_bool(d, "ldro_detected", required=False),
        ldro_match=_check_bool(d, "ldro_match", required=False),
        ldro_lsb0_fraction=_check_float(d, "ldro_lsb0_fraction", lo=0, hi=1),
        ldro_n_samples=_check_uint(d, "ldro_n_samples", required=False, hi=2**16),
    )


def validate_carrier(d: Mapping[str, Any]) -> Carrier:
    sw = _check_uint(d, "sync_word", lo=0, hi=255)
    sf = _check_uint(d, "sf", lo=5, hi=12)
    bw = _check_uint(d, "bw", lo=0, hi=2_000_000)
    cr = _check_uint(d, "cr", lo=1, hi=4)
    ldro_cfg = _check_bool(d, "ldro_cfg")
    if bw not in KNOWN_BWS:
        raise SchemaError(f"carrier.bw={bw} not in known bandwidths")
    assert sw is not None and sf is not None and bw is not None and cr is not None
    assert ldro_cfg is not None
    return Carrier(sync_word=sw, sf=sf, bw=bw, cr=cr, ldro_cfg=ldro_cfg)


def validate_diversity(d: Mapping[str, Any]) -> Diversity:
    n = _check_uint(d, "n_candidates", lo=0, hi=64)
    decoded_channel = _check_uint(d, "decoded_channel", required=False, hi=2**16)
    rx_channels = d.get("rx_channels", [])
    if not isinstance(rx_channels, (list, tuple)):
        raise SchemaError("diversity.rx_channels must be array")
    snr_db = d.get("snr_db", [])
    if not isinstance(snr_db, (list, tuple)):
        raise SchemaError("diversity.snr_db must be array")
    crc_mask = _check_uint(d, "crc_mask", hi=2**32)
    gap_us = _check_uint(d, "gap_us", hi=2**32)
    source_ids = d.get("source_ids", [])
    if not isinstance(source_ids, (list, tuple)):
        raise SchemaError("diversity.source_ids must be array")
    rx_sources = d.get("rx_sources", [])
    if not isinstance(rx_sources, (list, tuple)):
        raise SchemaError("diversity.rx_sources must be array")
    assert n is not None and crc_mask is not None and gap_us is not None
    return Diversity(
        n_candidates=n,
        decoded_channel=decoded_channel,
        rx_channels=tuple(int(x) for x in rx_channels),
        snr_db=tuple(float(x) for x in snr_db),
        crc_mask=crc_mask,
        gap_us=gap_us,
        source_ids=tuple(str(x) for x in source_ids),
        rx_sources=tuple(str(x) for x in rx_sources),
    )


def validate_protocol_annotation(d: Mapping[str, Any]) -> ProtocolAnnotation:
    name = _check_text(d, "name")
    ok = _check_bool(d, "ok")
    fields = d.get("fields", {})
    if not isinstance(fields, Mapping):
        raise SchemaError("protocol.fields must be map")
    error = _check_text(d, "error", required=False)
    assert name is not None and ok is not None
    return ProtocolAnnotation(name=name, ok=ok, fields=dict(fields), error=error)


def validate_lora_frame(d: Mapping[str, Any]) -> LoraFrame:
    if d.get("type") != "lora_frame":
        raise SchemaError(f"type must be 'lora_frame', got {d.get('type')!r}")
    payload = _check_bytes(d, "payload", maxlen=MAX_PAYLOAD)
    payload_len = _check_uint(d, "payload_len", hi=MAX_PAYLOAD)
    assert payload is not None and payload_len is not None
    if payload_len != len(payload):
        raise SchemaError(
            f"payload_len={payload_len} mismatch with len(payload)={len(payload)}"
        )
    seq = _check_uint(d, "seq", hi=2**32)
    assert seq is not None
    crc_valid = _check_bool(d, "crc_valid")
    carrier_d = d.get("carrier")
    if not isinstance(carrier_d, Mapping):
        raise SchemaError("lora_frame.carrier must be map")
    # Top-level `cr` mirrors carrier.cr; the C++ FrameSink emits cr only
    # inside `carrier`, so fall back to carrier when the top-level mirror
    # is absent (parallels the phy_overlay pattern below). Without this,
    # every lora_frame from lora_trx is silently dropped at L3 → bridge
    # never sees decoded ADVERTs → contact-learning regression.
    cr_src: Mapping[str, Any] = d if "cr" in d else carrier_d
    cr_top = _check_uint(cr_src, "cr", lo=1, hi=4)
    is_downchirp = _check_bool(d, "is_downchirp")
    payload_hash = _check_uint(d, "payload_hash", hi=2**64)
    id_ = _check_text(d, "id", maxlen=36)
    if id_ is not None and not _UUID_RE.match(id_):
        raise SchemaError(f"id {id_!r} is not a UUID-shaped string")
    ts = _check_text(d, "ts", maxlen=64)
    phy_d = d.get("phy")
    if not isinstance(phy_d, Mapping):
        raise SchemaError("lora_frame.phy must be map")
    # Real lora_trx puts frame identity (sf/bw/cr/sync_word) in `carrier`
    # only and DSP state (snr_db, noise_floor_db, …) in `phy`. CBOR-SCHEMA.md
    # documents both maps as duplicating these fields, but in practice phy
    # may omit them. Build an overlay dict so validate_phy still gets every
    # required field. crc_valid is the top-level mirror.
    phy_overlay: dict[str, Any] = dict(phy_d)
    for k in ("sf", "bw", "cr", "sync_word"):
        if k not in phy_overlay and k in carrier_d:
            phy_overlay[k] = carrier_d[k]
    if "crc_valid" not in phy_overlay and "crc_valid" in d:
        phy_overlay["crc_valid"] = d["crc_valid"]
    phy_d = phy_overlay
    div_d = d.get("diversity")
    diversity = validate_diversity(div_d) if isinstance(div_d, Mapping) else None
    proto_d = d.get("protocol")
    protocol = (
        validate_protocol_annotation(proto_d) if isinstance(proto_d, Mapping) else None
    )
    direction = _check_text(d, "direction", required=False, default="rx") or "rx"
    if direction not in ("rx", "tx"):
        raise SchemaError(f"direction must be rx or tx, got {direction!r}")
    rx_channel = _check_uint(d, "rx_channel", required=False, hi=2**16)
    decode_label = _check_text(d, "decode_label", required=False, maxlen=64)
    device = _check_text(d, "device", required=False, maxlen=64)
    # ``source`` is required on lora-core output, optional on raw lora_trx :5556.
    # Validator accepts either; daemon stamps it before fanout.
    source = _check_text(d, "source", required=False, maxlen=64) or ""
    assert (
        crc_valid is not None
        and cr_top is not None
        and is_downchirp is not None
        and payload_hash is not None
        and id_ is not None
        and ts is not None
    )
    return LoraFrame(
        type="lora_frame",
        ts=ts,
        seq=seq,
        payload=payload,
        payload_len=payload_len,
        crc_valid=crc_valid,
        cr=cr_top,
        is_downchirp=is_downchirp,
        payload_hash=payload_hash,
        id=id_,
        phy=validate_phy(phy_d),
        carrier=validate_carrier(carrier_d),
        source=source,
        direction=direction,
        rx_channel=rx_channel,
        decode_label=decode_label,
        device=device,
        diversity=diversity,
        protocol=protocol,
    )


def validate_status(d: Mapping[str, Any]) -> Status:
    if d.get("type") != "status":
        raise SchemaError(f"type must be 'status', got {d.get('type')!r}")
    ts = _check_text(d, "ts", maxlen=64)
    phy_d = d.get("phy", {})
    if not isinstance(phy_d, Mapping):
        raise SchemaError("status.phy must be map")
    rx_gain = _check_float(phy_d, "rx_gain", required=True, lo=-200, hi=200)
    tx_gain = _check_float(phy_d, "tx_gain", required=True, lo=-200, hi=200)
    frames_d = d.get("frames", {})
    if not isinstance(frames_d, Mapping):
        raise SchemaError("status.frames must be map")
    total = _check_uint(frames_d, "total", hi=2**63)
    crc_ok = _check_uint(frames_d, "crc_ok", hi=2**63)
    crc_fail = _check_uint(frames_d, "crc_fail", hi=2**63)
    rx_overflows = _check_uint(d, "rx_overflows", hi=2**63)
    assert (
        ts is not None
        and rx_gain is not None
        and tx_gain is not None
        and total is not None
        and crc_ok is not None
        and crc_fail is not None
        and rx_overflows is not None
    )
    return Status(
        type="status",
        ts=ts,
        phy=StatusPhy(rx_gain=rx_gain, tx_gain=tx_gain),
        frames=StatusFrames(total=total, crc_ok=crc_ok, crc_fail=crc_fail),
        rx_overflows=rx_overflows,
    )


def validate_spectrum(d: Mapping[str, Any]) -> Spectrum:
    t = d.get("type")
    if t not in ("spectrum", "spectrum_tx"):
        raise SchemaError(f"type must be spectrum or spectrum_tx, got {t!r}")
    fft_size = _check_uint(d, "fft_size", lo=1, hi=MAX_BINS_FFT_SIZE)
    assert fft_size is not None
    bins = _check_bytes(d, "bins", maxlen=MAX_BINS_FFT_SIZE * 4)
    assert bins is not None
    if len(bins) != fft_size * 4:
        raise SchemaError(
            f"spectrum.bins length {len(bins)} != fft_size*4={fft_size * 4}"
        )
    center_freq = _check_float(d, "center_freq", required=True, lo=0)
    sample_rate = _check_float(d, "sample_rate", required=True, lo=0)
    assert center_freq is not None and sample_rate is not None
    return Spectrum(
        type=t,
        bins=bins,
        fft_size=fft_size,
        center_freq=center_freq,
        sample_rate=sample_rate,
    )


def validate_subscribe(d: Mapping[str, Any]) -> Subscribe:
    if d.get("type") != "subscribe":
        raise SchemaError(f"type must be 'subscribe', got {d.get('type')!r}")
    sw = d.get("sync_word")
    if sw is not None:
        if not isinstance(sw, (list, tuple)):
            raise SchemaError("subscribe.sync_word must be array")
        if len(sw) > 256:
            raise SchemaError("subscribe.sync_word too long (>256)")
        for v in sw:
            if not isinstance(v, int) or v < 0 or v > 255:
                raise SchemaError(f"subscribe.sync_word entry out of range: {v}")
        sw = tuple(int(v) for v in sw)
    src = d.get("source")
    if src is not None:
        if not isinstance(src, (list, tuple)):
            raise SchemaError("subscribe.source must be array")
        if len(src) > 64:
            raise SchemaError("subscribe.source too long (>64)")
        for v in src:
            if not isinstance(v, str) or len(v) > MAX_TEXT_FIELD:
                raise SchemaError("subscribe.source entry must be short string")
        src = tuple(str(v) for v in src)
    return Subscribe(type="subscribe", sync_word=sw, source=src)


def validate_lora_tx(d: Mapping[str, Any]) -> LoraTx:
    if d.get("type") != "lora_tx":
        raise SchemaError(f"type must be 'lora_tx', got {d.get('type')!r}")
    payload = _check_bytes(d, "payload", maxlen=255)
    assert payload is not None
    return LoraTx(
        type="lora_tx",
        payload=payload,
        seq=_check_uint(d, "seq", required=False, hi=2**32),
        cr=_check_uint(d, "cr", required=False, lo=1, hi=4),
        sync_word=_check_uint(d, "sync_word", required=False, hi=255),
        preamble_len=_check_uint(d, "preamble_len", required=False, lo=4, hi=2**16),
        repeat=_check_uint(d, "repeat", required=False, lo=1, hi=1024),
        gap_ms=_check_uint(d, "gap_ms", required=False, hi=60_000),
        dry_run=_check_bool(d, "dry_run", required=False),
        source=_check_text(d, "source", required=False, maxlen=64),
    )


def validate_lora_tx_ack(d: Mapping[str, Any]) -> LoraTxAck:
    if d.get("type") != "lora_tx_ack":
        raise SchemaError(f"type must be 'lora_tx_ack', got {d.get('type')!r}")
    seq = _check_uint(d, "seq", hi=2**32)
    ok = _check_bool(d, "ok")
    error = _check_text(d, "error", required=False)
    assert seq is not None and ok is not None
    return LoraTxAck(type="lora_tx_ack", seq=seq, ok=ok, error=error)


def validate_multisf_detect(d: Mapping[str, Any]) -> MultisfDetect:
    if d.get("type") != "multisf_detect":
        raise SchemaError(f"type must be 'multisf_detect', got {d.get('type')!r}")
    ts = _check_text(d, "ts")
    sf = _check_uint(d, "sf", lo=5, hi=12)
    bin_ = _check_uint(d, "bin", hi=2**16)
    assert ts is not None and sf is not None and bin_ is not None
    return MultisfDetect(type="multisf_detect", ts=ts, sf=sf, bin=bin_)


def validate_multisf_sync(d: Mapping[str, Any]) -> MultisfSync:
    if d.get("type") != "multisf_sync":
        raise SchemaError(f"type must be 'multisf_sync', got {d.get('type')!r}")
    ts = _check_text(d, "ts")
    sf = _check_uint(d, "sf", lo=5, hi=12)
    cfo_int = _check_int(d, "cfo_int", required=True, lo=-(2**16), hi=2**16)
    cfo_frac = _check_float(d, "cfo_frac", required=True, lo=-1e3, hi=1e3)
    snr_db = _check_float(d, "snr_db", required=True, lo=-200, hi=200)
    assert (
        ts is not None
        and sf is not None
        and cfo_int is not None
        and cfo_frac is not None
        and snr_db is not None
    )
    return MultisfSync(
        type="multisf_sync",
        ts=ts,
        sf=sf,
        cfo_int=cfo_int,
        cfo_frac=cfo_frac,
        snr_db=snr_db,
    )


def validate_multisf_frame(d: Mapping[str, Any]) -> MultisfFrame:
    if d.get("type") != "multisf_frame":
        raise SchemaError(f"type must be 'multisf_frame', got {d.get('type')!r}")
    ts = _check_text(d, "ts")
    sf = _check_uint(d, "sf", lo=5, hi=12)
    crc_ok = _check_bool(d, "crc_ok")
    length = _check_uint(d, "len", hi=MAX_PAYLOAD)
    cr = _check_uint(d, "cr", lo=0, hi=4)
    snr_db = _check_float(d, "snr_db", required=True, lo=-200, hi=200)
    assert (
        ts is not None
        and sf is not None
        and crc_ok is not None
        and length is not None
        and cr is not None
        and snr_db is not None
    )
    return MultisfFrame(
        type="multisf_frame",
        ts=ts,
        sf=sf,
        crc_ok=crc_ok,
        len=length,
        cr=cr,
        snr_db=snr_db,
    )


def validate_wideband_sweep(d: Mapping[str, Any]) -> WidebandSweep:
    if d.get("type") != "wideband_sweep":
        raise SchemaError(f"type must be 'wideband_sweep', got {d.get('type')!r}")
    ts = _check_text(d, "ts")
    sweep = _check_uint(d, "sweep", hi=2**32)
    tainted = _check_bool(d, "tainted")
    overflows = _check_uint(d, "overflows", hi=2**32)
    zero_calls = _check_uint(d, "zero_calls", hi=2**32)
    total_calls = _check_uint(d, "total_calls", hi=2**32)
    duration_ms = _check_uint(d, "duration_ms", hi=2**32)
    n_snapshots = _check_uint(d, "n_snapshots", hi=2**32)
    n_hot = _check_uint(d, "n_hot", hi=2**16)
    n_active = _check_uint(d, "n_active", hi=2**16)
    max_slots = _check_uint(d, "max_slots", hi=2**16)
    assert all(
        v is not None
        for v in (
            ts,
            sweep,
            tainted,
            overflows,
            zero_calls,
            total_calls,
            duration_ms,
            n_snapshots,
            n_hot,
            n_active,
            max_slots,
        )
    )
    assert ts is not None
    return WidebandSweep(
        type="wideband_sweep",
        ts=ts,
        sweep=sweep,  # pyright: ignore[reportArgumentType]
        tainted=tainted,  # pyright: ignore[reportArgumentType]
        overflows=overflows,  # pyright: ignore[reportArgumentType]
        zero_calls=zero_calls,  # pyright: ignore[reportArgumentType]
        total_calls=total_calls,  # pyright: ignore[reportArgumentType]
        duration_ms=duration_ms,  # pyright: ignore[reportArgumentType]
        n_snapshots=n_snapshots,  # pyright: ignore[reportArgumentType]
        n_hot=n_hot,  # pyright: ignore[reportArgumentType]
        n_active=n_active,  # pyright: ignore[reportArgumentType]
        max_slots=max_slots,  # pyright: ignore[reportArgumentType]
    )


def validate_wideband_slot(d: Mapping[str, Any]) -> WidebandSlot:
    if d.get("type") != "wideband_slot":
        raise SchemaError(f"type must be 'wideband_slot', got {d.get('type')!r}")
    ts = _check_text(d, "ts")
    action = _check_text(d, "action")
    if action not in ("activate", "deactivate"):
        raise SchemaError("wideband_slot.action must be activate|deactivate")
    slot = _check_uint(d, "slot", hi=2**16)
    channel = _check_uint(d, "channel", hi=2**32)
    freq = _check_float(d, "freq", required=True, lo=0)
    bw = _check_uint(d, "bw", hi=2_000_000)
    if bw not in KNOWN_BWS:
        raise SchemaError(f"wideband_slot.bw={bw} not in known bandwidths")
    assert (
        ts is not None
        and action is not None
        and slot is not None
        and channel is not None
        and freq is not None
        and bw is not None
    )
    return WidebandSlot(
        type="wideband_slot",
        ts=ts,
        action=action,
        slot=slot,
        channel=channel,
        freq=freq,
        bw=bw,
    )


# ---- dispatcher ----------------------------------------------------------


def dispatch(d: Mapping[str, Any]) -> Any:
    """Look at ``d['type']`` and route to the matching validator.

    Returns the typed dataclass on success, or ``None`` on any failure.
    Never raises in the hot path.
    """
    if not isinstance(d, Mapping):
        return None
    t = d.get("type")
    if not isinstance(t, str) or t not in KNOWN_TYPES:
        return None
    try:
        if t == "lora_frame":
            return validate_lora_frame(d)
        if t == "status":
            return validate_status(d)
        if t in ("spectrum", "spectrum_tx"):
            return validate_spectrum(d)
        if t == "subscribe":
            return validate_subscribe(d)
        if t == "lora_tx":
            return validate_lora_tx(d)
        if t == "lora_tx_ack":
            return validate_lora_tx_ack(d)
        if t == "multisf_detect":
            return validate_multisf_detect(d)
        if t == "multisf_sync":
            return validate_multisf_sync(d)
        if t == "multisf_frame":
            return validate_multisf_frame(d)
        if t == "wideband_sweep":
            return validate_wideband_sweep(d)
        if t == "wideband_slot":
            return validate_wideband_slot(d)
        # ``config`` validation lands in Phase 2 alongside the daemon.
        return None
    except SchemaError:
        return None
    except Exception:
        # Defence in depth: never propagate any unexpected exception
        # out of the validation boundary.
        return None
