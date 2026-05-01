#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.bridges.meshcore.state`.

Covers:

* CBOR EEPROM round-trip + atomic write semantics.
* No-JSON-fallback policy: a stale ``config.json`` raises
  :class:`LegacyJsonEepromError` rather than being silently read.
* :class:`BridgeState` startup-config snapshot / apply / persist.
"""

from __future__ import annotations

import json
from pathlib import Path

import cbor2
import conftest
import pytest

from lora.bridges.meshcore.state import (
    EEPROM_SCHEMA_VERSION,
    LegacyJsonEepromError,
    initialize_eeprom_from_settings,
    load_eeprom,
    write_eeprom,
)


class TestEepromCBORRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        cfg = {
            "version": EEPROM_SCHEMA_VERSION,
            "identity": {"name": "alice", "lat_e6": 0, "lon_e6": 0},
        }
        write_eeprom(path, cfg)
        assert load_eeprom(path) == cfg

    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_eeprom(tmp_path / "absent.cbor") == {}

    def test_corrupt_cbor_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        path.write_bytes(b"not cbor at all")
        assert load_eeprom(path) == {}

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        path.write_bytes(cbor2.dumps({"version": 99}))
        assert load_eeprom(path) == {}

    def test_legacy_json_sibling_raises(self, tmp_path: Path) -> None:
        cbor_path = tmp_path / "config.cbor"
        json_path = tmp_path / "config.json"
        json_path.write_text(
            json.dumps({"version": EEPROM_SCHEMA_VERSION, "identity": {}})
        )
        with pytest.raises(LegacyJsonEepromError):
            load_eeprom(cbor_path)


class TestAtomicWrite:
    def test_no_partial_files_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        write_eeprom(path, {"version": EEPROM_SCHEMA_VERSION, "identity": {}})
        # Only the dest file should exist; no .tmp.* siblings.
        siblings = list(tmp_path.iterdir())
        assert siblings == [path]

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        write_eeprom(path, {"version": EEPROM_SCHEMA_VERSION, "k": 1})
        write_eeprom(path, {"version": EEPROM_SCHEMA_VERSION, "k": 2})
        assert load_eeprom(path)["k"] == 2

    def test_initialise_from_settings(self, tmp_path: Path) -> None:
        path = tmp_path / "config.cbor"
        cfg = initialize_eeprom_from_settings(
            {
                "name": "bob",
                "lat": 53.55,
                "lon": 10.0,
                "client_repeat": 1,
                "path_hash_mode": 2,
                "autoadd_config": 0xAA,
                "autoadd_max_hops": 7,
            },
            path,
        )
        loaded = load_eeprom(path)
        assert loaded == cfg
        assert loaded["identity"]["name"] == "bob"
        assert loaded["identity"]["lat_e6"] == 53_550_000
        assert loaded["routing"]["client_repeat"] == 1
        assert loaded["routing"]["path_hash_mode"] == 2
        assert loaded["contacts"]["autoadd_config"] == 0xAA
        assert loaded["contacts"]["autoadd_max_hops"] == 7


class TestBridgeStateStartup:
    def test_make_state_default_pubkey(self) -> None:
        state = conftest.make_state()
        assert isinstance(state.pub_key, bytes)
        assert len(state.pub_key) == 32

    def test_apply_startup_restores_name(self) -> None:
        state = conftest.make_state()
        original = state.name
        state.name = "mutated"
        state.apply_startup_config()
        assert state.name == original

    def test_persist_then_load(self, tmp_path: Path) -> None:
        state = conftest.make_state(eeprom_path=tmp_path / "config.cbor")
        state.name = "after-persist"
        state.lat_e6 = 53_550_000
        state.persist_config()
        loaded = load_eeprom(state.eeprom_path)
        assert loaded["identity"]["name"] == "after-persist"
        assert loaded["identity"]["lat_e6"] == 53_550_000

    def test_persist_updates_startup_mirror(self, tmp_path: Path) -> None:
        state = conftest.make_state(eeprom_path=tmp_path / "config.cbor")
        state.name = "post-persist"
        state.persist_config()
        # apply_startup_config should now restore the persisted value.
        state.name = "transient"
        state.apply_startup_config()
        assert state.name == "post-persist"

    def test_reset_config_unlinks(self, tmp_path: Path) -> None:
        state = conftest.make_state(eeprom_path=tmp_path / "config.cbor")
        state.persist_config()
        assert state.eeprom_path.exists()
        state.reset_config()
        assert not state.eeprom_path.exists()


class TestStatsAndBuilders:
    def test_build_self_info_layout(self) -> None:
        state = conftest.make_state(name="self-info-test")
        out = state.build_self_info()
        assert out[0] == 0x05  # RESP_SELF_INFO
        # last bytes are the truncated name
        assert out.endswith(b"self-info-test")

    def test_build_device_info_size(self) -> None:
        state = conftest.make_state()
        out = state.build_device_info()
        # 1 (resp) + 1 (fw_ver) + 1 (max_contacts) + 1 (max_channels) +
        # 4 (ble_pin) + 12 (fw_build) + 40 (model) + 20 (version) +
        # 1 (client_repeat) + 1 (path_hash_mode) = 82 bytes
        assert len(out) == 82
        assert out[0] == 0x0D

    def test_build_msg_sent_routing_byte(self) -> None:
        state = conftest.make_state()
        flood = state.build_msg_sent(flood=True, ack_hash=b"\x01\x02\x03\x04")
        direct = state.build_msg_sent(flood=False, ack_hash=b"\x05\x06\x07\x08")
        assert flood[1] == 1
        assert direct[1] == 0
        assert flood[2:6] == b"\x01\x02\x03\x04"
        assert direct[2:6] == b"\x05\x06\x07\x08"
