#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""DuckDB schema for the lora-core daemon storage layer.

All ``CREATE TABLE`` statements are idempotent (``IF NOT EXISTS``) so
:func:`apply_schema` is safe to call on every daemon start. Forward
schema changes are layered on top via :func:`migrate` using
``ALTER TABLE ADD COLUMN IF NOT EXISTS`` (additive only).

Phase 2 introduces SCHEMA_VERSION = 1. The ``schema_meta`` table
records installed versions so future migrations can branch on the
historical state of an existing database file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger("lora.storage")

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

# lora_frames: every typed lora_frame the daemon ingests, including the
# optional protocol annotation (meshcore / lorawan / ...) and diversity
# sub-map. Columns map 1:1 to LoraFrame / Phy / Carrier / Diversity /
# ProtocolAnnotation in :mod:`lora.core.types`. The PRIMARY KEY on
# ``id`` deduplicates retransmissions of the same frame across upstream
# sources at write time.
CREATE_LORA_FRAMES = """
CREATE TABLE IF NOT EXISTS lora_frames (
    ts            TIMESTAMPTZ NOT NULL,
    seq           UBIGINT NOT NULL,
    id            TEXT NOT NULL,
    source        TEXT NOT NULL,
    sf            UTINYINT,
    bw            UINTEGER,
    cr            UTINYINT,
    sync_word     UTINYINT,
    payload       BLOB,
    payload_len   UINTEGER,
    crc_valid     BOOLEAN,
    payload_hash  UBIGINT,
    snr_db        DOUBLE,
    snr_db_td     DOUBLE,
    noise_floor_db DOUBLE,
    peak_db       DOUBLE,
    cfo_int       DOUBLE,
    cfo_frac      DOUBLE,
    sfo_hat       DOUBLE,
    ppm_error     DOUBLE,
    sample_rate   DOUBLE,
    channel_freq  DOUBLE,
    rx_channel    UINTEGER,
    decode_label  TEXT,
    device        TEXT,
    diversity_n              UINTEGER,
    diversity_gap_us         UINTEGER,
    diversity_crc_mask       UINTEGER,
    diversity_decoded_channel UINTEGER,
    diversity_rx_sources     TEXT[],
    protocol_name        TEXT,
    protocol_ok          BOOLEAN,
    protocol_error       TEXT,
    mc_route             TEXT,
    mc_payload_type      TEXT,
    mc_path              BLOB,
    mc_path_len          UINTEGER,
    mc_sender_pubkey     BLOB,
    mc_sender_name       TEXT,
    mc_decrypted         BOOLEAN,
    mc_decrypted_text    TEXT,
    mc_channel           TEXT,
    lorawan_mtype        TEXT,
    lorawan_dev_addr     TEXT,
    lorawan_fcnt         UINTEGER,
    lorawan_fport        UTINYINT,
    lorawan_is_uplink    BOOLEAN,
    ldro_cfg             BOOLEAN,
    ldro_detected        BOOLEAN,
    ldro_match           BOOLEAN,
    PRIMARY KEY (id)
);
"""

CREATE_INDEX_LORA_FRAMES = (
    "CREATE INDEX IF NOT EXISTS idx_lora_frames_ts_source "
    "ON lora_frames(ts DESC, source, sync_word);"
)

# Heartbeat table — note the renamed table (``status_heartbeats`` vs the
# legacy ``trx_status``) and the writer-internal ``writer_dropped``
# column. ``writer_dropped`` is filled by the writer thread from its
# own bounded-queue overflow counter; upstream Status messages do not
# carry it. ``source`` matches the convention on every other telemetry
# table (``lora_frames``, ``multisf_*``, ``wideband_*``) — the upstream's
# logical name from ``[[core.upstream]]`` config — so multi-radio
# heartbeats are attributable.
CREATE_STATUS_HEARTBEATS = """
CREATE TABLE IF NOT EXISTS status_heartbeats (
    ts                 TIMESTAMPTZ NOT NULL,
    rx_gain            DOUBLE,
    tx_gain            DOUBLE,
    frames_total       UBIGINT,
    frames_crc_ok      UBIGINT,
    frames_crc_fail    UBIGINT,
    rx_overflows       UBIGINT,
    writer_dropped     UBIGINT,
    source             TEXT
);
"""

# Telemetry tables — ported from the legacy lora_duckdb.py
# create_tables(). The column names are preserved verbatim from the
# legacy schema (so existing offline queries keep working) and a new
# ``source TEXT`` column is added for multi-source attribution.
CREATE_MULTISF_DETECT = """
CREATE TABLE IF NOT EXISTS multisf_detect (
    ts     TIMESTAMPTZ NOT NULL,
    sf     UTINYINT    NOT NULL,
    bin    UINTEGER    NOT NULL,
    source TEXT
);
"""

CREATE_MULTISF_SYNC = """
CREATE TABLE IF NOT EXISTS multisf_sync (
    ts       TIMESTAMPTZ NOT NULL,
    sf       UTINYINT    NOT NULL,
    cfo_int  INTEGER,
    cfo_frac DOUBLE,
    snr_db   DOUBLE,
    source   TEXT
);
"""

CREATE_MULTISF_FRAME = """
CREATE TABLE IF NOT EXISTS multisf_frame (
    ts     TIMESTAMPTZ NOT NULL,
    sf     UTINYINT    NOT NULL,
    crc_ok BOOLEAN     NOT NULL,
    len    UINTEGER    NOT NULL,
    cr     UTINYINT    NOT NULL,
    snr_db DOUBLE,
    source TEXT
);
"""

CREATE_WIDEBAND_SWEEP = """
CREATE TABLE IF NOT EXISTS wideband_sweep (
    ts           TIMESTAMPTZ NOT NULL,
    sweep        UINTEGER    NOT NULL,
    tainted      BOOLEAN     NOT NULL,
    overflows    UINTEGER    NOT NULL,
    zero_calls   UINTEGER    NOT NULL,
    total_calls  UINTEGER    NOT NULL,
    duration_ms  UINTEGER    NOT NULL,
    n_snapshots  UINTEGER    NOT NULL,
    n_hot        UINTEGER    NOT NULL,
    n_active     UINTEGER    NOT NULL,
    max_slots    UINTEGER    NOT NULL,
    source       TEXT
);
"""

CREATE_WIDEBAND_SLOT = """
CREATE TABLE IF NOT EXISTS wideband_slot (
    ts      TIMESTAMPTZ NOT NULL,
    action  VARCHAR     NOT NULL,
    slot    UINTEGER    NOT NULL,
    channel UINTEGER    NOT NULL,
    freq    DOUBLE      NOT NULL,
    bw      UINTEGER    NOT NULL,
    source  TEXT
);
"""

CREATE_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version      INTEGER PRIMARY KEY,
    installed_at TIMESTAMPTZ
);
"""

ALL_CREATE: list[str] = [
    CREATE_LORA_FRAMES,
    CREATE_INDEX_LORA_FRAMES,
    CREATE_STATUS_HEARTBEATS,
    CREATE_MULTISF_DETECT,
    CREATE_MULTISF_SYNC,
    CREATE_MULTISF_FRAME,
    CREATE_WIDEBAND_SWEEP,
    CREATE_WIDEBAND_SLOT,
    CREATE_SCHEMA_META,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def archive_if_pre_source_schema(db_path: Path) -> Path | None:
    """Archive ``db_path`` when its ``status_heartbeats`` table predates the
    ``source`` column (SCHEMA_VERSION 2).

    Returns the archive path on success, ``None`` when no action was taken
    (file missing, schema already current, or DB locked / unreadable).

    The migration policy for SCHEMA_VERSION 2 is "fresh schema, archive
    old DB" — DuckDB's ``ALTER TABLE ADD COLUMN`` would also work but the
    project preference is to keep the live DB schema-pure and let offline
    analyses pull from the archive on demand.
    """
    if not db_path.exists():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error:
        return None  # locked or unreadable; let main connect surface the error
    try:
        rows = con.execute("PRAGMA table_info('status_heartbeats')").fetchall()
    except duckdb.Error:
        return None
    finally:
        con.close()
    if not rows:
        return None  # table absent — apply_schema will create it fresh
    column_names = {row[1] for row in rows}  # PRAGMA columns: cid, name, type, ...
    if "source" in column_names:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = db_path.with_name(f"{db_path.name}.pre_source_{stamp}.bak")
    db_path.rename(archive)
    log.warning(
        "storage: archived %s -> %s (schema bump: status_heartbeats.source)",
        db_path,
        archive,
    )
    return archive


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Run all CREATE statements (idempotent) and stamp ``schema_meta``.

    Safe to call repeatedly: every statement uses ``IF NOT EXISTS`` and
    the version row insert is ``ON CONFLICT DO NOTHING``.
    """
    for stmt in ALL_CREATE:
        con.execute(stmt)
    con.execute(
        "INSERT INTO schema_meta (version, installed_at) "
        "VALUES (?, current_timestamp) ON CONFLICT (version) DO NOTHING",
        [SCHEMA_VERSION],
    )
    migrate(con)


def migrate(con: duckdb.DuckDBPyConnection) -> None:
    """Apply additive migrations keyed off ``schema_meta.version``.

    Phase 2 has only one schema version so this is a no-op; the function
    exists so future versions can layer ``ALTER TABLE ADD COLUMN
    IF NOT EXISTS`` calls behind a single entry point.
    """
    # Future: branch on installed versions, e.g.
    #   if not _has_version(con, 2):
    #       con.execute("ALTER TABLE lora_frames ADD COLUMN IF NOT EXISTS ...")
    #       con.execute("INSERT INTO schema_meta VALUES (2, current_timestamp)")
    _ = con  # silence unused-arg lint until first migration lands
