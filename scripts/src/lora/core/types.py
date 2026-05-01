#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Typed dataclasses mirroring the CBOR wire schemas in CBOR-SCHEMA.md.

These are the canonical in-process representations that the daemon's
validators (:mod:`lora.core.schema`) produce after L1+L2+L3 input
validation. Consumers receive immutable, fully-typed records and
never touch raw CBOR dicts in their hot paths.

Phase 1 covers the lora_trx wire schemas (lora_frame, status, spectrum,
subscribe, lora_tx, lora_tx_ack, multisf_*, wideband_*) plus the
forward-compat ``ProtocolAnnotation`` and the new ``source`` field
introduced by the multi-source daemon (see spec Section 3). Phase 2
wires these into the daemon, so we keep them additive: every legacy
field is preserved and every new field is optional with a sensible
default.

All dataclasses are ``frozen=True, slots=True``: immutable, cheap, and
hashable when their components are.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# ---- lora_frame ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Phy:
    """DSP state for one decoded frame.

    Mandatory fields (sf, bw, cr, sync_word, crc_valid) are populated
    on every frame. Everything else is optional and only present when
    the originating decoder emits it (see CBOR-SCHEMA.md notes on
    optional fields).
    """

    sf: int
    bw: int
    cr: int
    sync_word: int
    crc_valid: bool
    snr_db: float | None = None
    noise_floor_db: float | None = None
    peak_db: float | None = None
    snr_db_td: float | None = None
    channel_freq: float | None = None
    decode_bw: float | None = None
    cfo_int: float | None = None
    cfo_frac: float | None = None
    sfo_hat: float | None = None
    sample_rate: float | None = None
    frequency_corrected: float | None = None
    ppm_error: float | None = None
    ldro_detected: bool | None = None
    ldro_match: bool | None = None
    ldro_lsb0_fraction: float | None = None
    ldro_n_samples: int | None = None


@dataclass(frozen=True, slots=True)
class Carrier:
    """Frame identity (sync_word, sf, bw, cr) — what the decoder thought
    the frame *is*. Distinct from ``Phy`` which captures DSP state.
    """

    sync_word: int
    sf: int
    bw: int
    cr: int
    ldro_cfg: bool


@dataclass(frozen=True, slots=True)
class Diversity:
    """Aggregated-frame diversity sub-map produced by lora_agg / lora-core."""

    n_candidates: int
    decoded_channel: int | None
    rx_channels: Sequence[int]
    snr_db: Sequence[float]
    crc_mask: int
    gap_us: int
    source_ids: Sequence[str]
    # Multi-source addition (spec Section 3): per-candidate source name.
    rx_sources: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class ProtocolAnnotation:
    """Decoder annotation attached to a lora_frame by lora-core's decode chain.

    See spec Section 3 for the per-decoder ``fields`` schemas. Empty
    ``fields`` plus ``ok=False`` means the decoder claimed the
    sync_word but parsing failed — the failure is recorded for later
    DuckDB queries rather than silently dropped.
    """

    name: str
    ok: bool
    fields: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LoraFrame:
    """Decoded RX frame (also used for outgoing TX with ``direction='tx'``).

    ``source`` is mandatory on output from :mod:`lora.daemon` (multi-source
    addition). Legacy upstream emitters (raw lora_trx :5556) do not set
    ``source``; the validator stamps the daemon-configured upstream name
    when it lifts a raw frame onto the typed bus.
    """

    type: str  # always "lora_frame"
    ts: str
    seq: int
    payload: bytes
    payload_len: int
    crc_valid: bool
    cr: int
    is_downchirp: bool
    payload_hash: int
    id: str
    phy: Phy
    carrier: Carrier
    source: str  # multi-source: matches one of [[core.upstream]].name
    direction: str = "rx"
    rx_channel: int | None = None
    decode_label: str | None = None
    device: str | None = None
    diversity: Diversity | None = None
    protocol: ProtocolAnnotation | None = None


# ---- status / spectrum ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusPhy:
    rx_gain: float
    tx_gain: float


@dataclass(frozen=True, slots=True)
class StatusFrames:
    total: int
    crc_ok: int
    crc_fail: int


@dataclass(frozen=True, slots=True)
class Status:
    """Periodic heartbeat from lora_trx / lora-core."""

    type: str  # always "status"
    ts: str
    phy: StatusPhy
    frames: StatusFrames
    rx_overflows: int


@dataclass(frozen=True, slots=True)
class Spectrum:
    """RX or TX waterfall snapshot (``type`` distinguishes ``spectrum``
    vs ``spectrum_tx``)."""

    type: str
    bins: bytes  # fft_size * float32 LE, dBFS
    fft_size: int
    center_freq: float
    sample_rate: float


# ---- subscribe / lora_tx / lora_tx_ack -----------------------------------


@dataclass(frozen=True, slots=True)
class Subscribe:
    """Client subscription / keepalive message."""

    type: str  # always "subscribe"
    sync_word: Sequence[int] | None = None
    # Multi-source addition (spec Section 3): optional per-source filter.
    # Empty / missing = all sources.
    source: Sequence[str] | None = None


@dataclass(frozen=True, slots=True)
class LoraTx:
    """TX request from a client to lora-core / lora_trx."""

    type: str  # always "lora_tx"
    payload: bytes
    seq: int | None = None
    cr: int | None = None
    sync_word: int | None = None
    preamble_len: int | None = None
    repeat: int | None = None
    gap_ms: int | None = None
    dry_run: bool | None = None
    # Multi-source addition (spec Section 3): which upstream to route through.
    # Absent => first [[core.upstream]] (daemon logs warn).
    source: str | None = None


@dataclass(frozen=True, slots=True)
class LoraTxAck:
    """Acknowledgement for a ``lora_tx`` request."""

    type: str  # always "lora_tx_ack"
    seq: int
    ok: bool
    error: str | None = None


# ---- multisf telemetry ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class MultisfDetect:
    type: str  # always "multisf_detect"
    ts: str
    sf: int
    bin: int


@dataclass(frozen=True, slots=True)
class MultisfSync:
    type: str  # always "multisf_sync"
    ts: str
    sf: int
    cfo_int: int
    cfo_frac: float
    snr_db: float


@dataclass(frozen=True, slots=True)
class MultisfFrame:
    type: str  # always "multisf_frame"
    ts: str
    sf: int
    crc_ok: bool
    len: int
    cr: int
    snr_db: float


# ---- wideband telemetry ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class WidebandSweep:
    type: str  # always "wideband_sweep"
    ts: str
    sweep: int
    tainted: bool
    overflows: int
    zero_calls: int
    total_calls: int
    duration_ms: int
    n_snapshots: int
    n_hot: int
    n_active: int
    max_slots: int


@dataclass(frozen=True, slots=True)
class WidebandSlot:
    type: str  # always "wideband_slot"
    ts: str
    action: str  # "activate" | "deactivate"
    slot: int
    channel: int
    freq: float
    bw: int
