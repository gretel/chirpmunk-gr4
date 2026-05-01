# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.storage.schema`.

Covers idempotent CREATE, version row, and required tables.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from lora.storage.schema import SCHEMA_VERSION, apply_schema

EXPECTED_TABLES = {
    "lora_frames",
    "status_heartbeats",
    "multisf_detect",
    "multisf_sync",
    "multisf_frame",
    "wideband_sweep",
    "wideband_slot",
    "schema_meta",
}


def test_apply_schema_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "schema.duckdb"
    con = duckdb.connect(str(db))
    try:
        apply_schema(con)
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        names = {r[0] for r in rows}
    finally:
        con.close()

    assert EXPECTED_TABLES.issubset(names), (
        f"missing: {EXPECTED_TABLES - names}; got: {names}"
    )


def test_apply_schema_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "schema.duckdb"
    con = duckdb.connect(str(db))
    try:
        apply_schema(con)
        # Second invocation must not raise.
        apply_schema(con)
        # Version table still exactly one row at SCHEMA_VERSION.
        version_rows = con.execute(
            "SELECT version FROM schema_meta ORDER BY version"
        ).fetchall()
    finally:
        con.close()

    assert version_rows == [(SCHEMA_VERSION,)]


def test_schema_version_row_present(tmp_path: Path) -> None:
    db = tmp_path / "schema.duckdb"
    con = duckdb.connect(str(db))
    try:
        apply_schema(con)
        rows = con.execute("SELECT version FROM schema_meta").fetchall()
    finally:
        con.close()

    assert rows == [(SCHEMA_VERSION,)]


def test_lora_frames_columns_present(tmp_path: Path) -> None:
    """Spot-check key columns including the new multi-source + protocol fields."""
    db = tmp_path / "schema.duckdb"
    con = duckdb.connect(str(db))
    try:
        apply_schema(con)
        cols = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'lora_frames'"
            ).fetchall()
        }
    finally:
        con.close()

    must_have = {
        "ts",
        "seq",
        "id",
        "source",
        "sf",
        "bw",
        "cr",
        "sync_word",
        "payload",
        "payload_hash",
        "diversity_n",
        "diversity_rx_sources",
        "protocol_name",
        "protocol_ok",
        "mc_route",
        "mc_decrypted_text",
        "lorawan_mtype",
        "ldro_cfg",
    }
    assert must_have.issubset(cols), f"missing: {must_have - cols}"
