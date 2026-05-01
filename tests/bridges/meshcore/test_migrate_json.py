#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.tools.migrate_meshcore_json`.

Round-trips a legacy JSON EEPROM through the migrator and verifies the
resulting CBOR file is loadable by :func:`lora.bridges.meshcore.state.load_eeprom`.
"""

from __future__ import annotations

import json
from pathlib import Path

import cbor2
import pytest

from lora.bridges.meshcore.state import EEPROM_SCHEMA_VERSION, load_eeprom
from lora.tools.migrate_meshcore_json import migrate

_REFERENCE = {
    "version": EEPROM_SCHEMA_VERSION,
    "identity": {"name": "alice", "lat_e6": 53_550_000, "lon_e6": 10_000_000},
    "routing": {"region_scope": "de-hh", "client_repeat": 1, "path_hash_mode": 2},
    "contacts": {
        "autoadd_config": 0xAA,
        "autoadd_max_hops": 5,
        "manual_add_contacts": 0,
    },
    "telemetry": {"telemetry_mode": 0, "adv_loc_policy": 0, "multi_acks": 0},
    "security": {"device_pin": 1234},
}


def _write_json(path: Path, data: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(data if data is not None else _REFERENCE))
    return path


class TestMigrate:
    def test_round_trip(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "config.json")
        dest = migrate(src)
        assert dest == tmp_path / "config.cbor"
        loaded = load_eeprom(dest)
        assert loaded == _REFERENCE

    def test_keeps_source_by_default(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "config.json")
        migrate(src)
        assert src.exists()

    def test_delete_source_flag(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "config.json")
        migrate(src, delete_source=True)
        assert not src.exists()

    def test_missing_source_errors(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            migrate(tmp_path / "no-such.json")

    def test_invalid_json_errors(self, tmp_path: Path) -> None:
        src = tmp_path / "config.json"
        src.write_text("not json at all")
        with pytest.raises(ValueError):
            migrate(src)

    def test_wrong_version_errors(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "config.json", {"version": 99, "x": 1})
        with pytest.raises(ValueError):
            migrate(src)

    def test_round_trip_through_cbor(self, tmp_path: Path) -> None:
        src = _write_json(tmp_path / "config.json")
        dest = migrate(src)
        # Manually decode CBOR to confirm format match.
        decoded = cbor2.loads(dest.read_bytes())
        assert decoded == _REFERENCE
