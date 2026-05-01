#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Pure conversion helpers from typed events to DuckDB row tuples.

Kept in a dedicated module so unit tests can exercise the flattening
logic without spinning up a full :class:`DuckDBWriter` thread.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from lora.core.types import LoraFrame, Status

log = logging.getLogger(__name__)


def parse_ts(s: str | None) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware :class:`datetime`.

    Accepts trailing ``Z`` (Zulu / UTC). On parse failure, logs a
    warning and returns ``datetime.now(timezone.utc)``.
    """
    if not s:
        log.warning("storage: empty ts, using now()")
        return datetime.now(timezone.utc)
    text = s
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError, TypeError:
        log.warning("storage: malformed ts %r, using now()", s)
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Stable column order. Single source of truth shared with the INSERT
# statement in :mod:`lora.storage.duckdb_writer` so column counts cannot
# silently diverge.
LORA_FRAME_COLUMNS: tuple[str, ...] = (
    "ts",
    "seq",
    "id",
    "source",
    "sf",
    "bw",
    "cr",
    "sync_word",
    "payload",
    "payload_len",
    "crc_valid",
    "payload_hash",
    "snr_db",
    "snr_db_td",
    "noise_floor_db",
    "peak_db",
    "cfo_int",
    "cfo_frac",
    "sfo_hat",
    "ppm_error",
    "sample_rate",
    "channel_freq",
    "rx_channel",
    "decode_label",
    "device",
    "diversity_n",
    "diversity_gap_us",
    "diversity_crc_mask",
    "diversity_decoded_channel",
    "diversity_rx_sources",
    "protocol_name",
    "protocol_ok",
    "protocol_error",
    "mc_route",
    "mc_payload_type",
    "mc_path",
    "mc_path_len",
    "mc_sender_pubkey",
    "mc_sender_name",
    "mc_decrypted",
    "mc_decrypted_text",
    "mc_channel",
    "lorawan_mtype",
    "lorawan_dev_addr",
    "lorawan_fcnt",
    "lorawan_fport",
    "lorawan_is_uplink",
    "ldro_cfg",
    "ldro_detected",
    "ldro_match",
)


def _mc_fields(fields: Mapping[str, object]) -> tuple[Any, ...]:
    """Pull the meshcore-specific protocol annotation columns."""
    return (
        fields.get("route"),
        fields.get("payload_type"),
        fields.get("path"),
        fields.get("path_len"),
        fields.get("sender_pubkey"),
        fields.get("sender_name"),
        fields.get("decrypted"),
        fields.get("decrypted_text"),
        fields.get("channel"),
    )


def _lorawan_fields(fields: Mapping[str, object]) -> tuple[Any, ...]:
    """Pull the LoRaWAN-specific protocol annotation columns."""
    return (
        fields.get("mtype"),
        fields.get("dev_addr"),
        fields.get("fcnt"),
        fields.get("fport"),
        fields.get("is_uplink"),
    )


_EMPTY_MC: tuple[Any, ...] = (None,) * 9
_EMPTY_LW: tuple[Any, ...] = (None,) * 5


def lora_frame_row(frame: LoraFrame) -> tuple[Any, ...]:
    """Flatten a :class:`LoraFrame` into a tuple matching ``lora_frames`` DDL.

    Optional sub-maps (``diversity``, ``protocol``) become NULLs when
    absent. The meshcore / lorawan protocol fields are pulled by name
    from ``frame.protocol.fields``; unknown decoders contribute NULLs
    for both blocks. Tuple length always equals ``len(LORA_FRAME_COLUMNS)``.
    """
    phy = frame.phy
    div = frame.diversity
    proto = frame.protocol

    if proto is not None and proto.name == "meshcore":
        mc_cols = _mc_fields(proto.fields)
    else:
        mc_cols = _EMPTY_MC

    if proto is not None and proto.name == "lorawan":
        lw_cols = _lorawan_fields(proto.fields)
    else:
        lw_cols = _EMPTY_LW

    if div is not None:
        rx_sources_list: list[str] | None = list(div.rx_sources)
        diversity_cols: tuple[Any, ...] = (
            div.n_candidates,
            div.gap_us,
            div.crc_mask,
            div.decoded_channel,
            rx_sources_list,
        )
    else:
        diversity_cols = (None, None, None, None, None)

    if proto is not None:
        proto_meta: tuple[Any, ...] = (proto.name, proto.ok, proto.error)
    else:
        proto_meta = (None, None, None)

    row: tuple[Any, ...] = (
        parse_ts(frame.ts),
        frame.seq,
        frame.id,
        frame.source,
        phy.sf,
        phy.bw,
        phy.cr,
        phy.sync_word,
        frame.payload,
        frame.payload_len,
        frame.crc_valid,
        frame.payload_hash,
        phy.snr_db,
        phy.snr_db_td,
        phy.noise_floor_db,
        phy.peak_db,
        phy.cfo_int,
        phy.cfo_frac,
        phy.sfo_hat,
        phy.ppm_error,
        phy.sample_rate,
        phy.channel_freq,
        frame.rx_channel,
        frame.decode_label,
        frame.device,
        *diversity_cols,
        *proto_meta,
        *mc_cols,
        *lw_cols,
        frame.carrier.ldro_cfg,
        phy.ldro_detected,
        phy.ldro_match,
    )

    if len(row) != len(LORA_FRAME_COLUMNS):  # pragma: no cover - invariant
        raise AssertionError(
            f"row width mismatch: row={len(row)} cols={len(LORA_FRAME_COLUMNS)}"
        )
    return row


def status_row(status: Status, writer_dropped: int) -> tuple[Any, ...]:
    """Flatten a :class:`Status` heartbeat into a row tuple.

    ``writer_dropped`` is the writer thread's local drop counter
    (bounded-queue overflow); it is not part of the upstream Status
    schema and is supplied by the caller (the writer itself).
    """
    return (
        parse_ts(status.ts),
        status.phy.rx_gain,
        status.phy.tx_gain,
        status.frames.total,
        status.frames.crc_ok,
        status.frames.crc_fail,
        status.rx_overflows,
        writer_dropped,
    )
