# SPDX-License-Identifier: ISC
"""Phase 2G strict no-compat validator tests.

These tests pin the fail-fast behaviour of
:func:`lora.core.config_typed.load_typed` and the corresponding
:meth:`lora.daemon.config.DaemonConfig.from_legacy_toml` wrapper.

Per spec Section 5, the legacy ``[aggregator]`` / ``[meshcore]`` keys
and the legacy single-string ``[core].upstream`` form are not aliased
into the new schema — they fail at load time with a clear hint.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from lora.core.config_typed import (
    TypedConfigError,
    load_typed,
)
from lora.daemon.config import DaemonConfig


def _minimal_valid(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid TOML dict.

    Identity paths point at a fresh tmp dir so the "creatable" check
    succeeds in CI sandboxes without writing real files.
    """
    tmp = Path(tempfile.mkdtemp(prefix="lora-typed-"))
    cfg: dict[str, Any] = {
        "core": {
            "listen": "127.0.0.1:5555",
            "fanout_queue_depth": 10000,
            "upstream": [
                {"name": "radio_868", "host": "127.0.0.1", "port": 5556},
            ],
            "aggregator": {"window_ms": 200, "max_candidates": 8},
            "decoders": {
                "enabled": ["meshcore", "lorawan", "raw"],
            },
            "identity": {
                "identity_file": str(tmp / "identity.bin"),
                "keys_dir": str(tmp / "keys"),
                "channels_dir": str(tmp / "channels"),
                "contacts_dir": str(tmp / "contacts"),
                "reload_interval_s": 5,
            },
            "storage": {
                "backend": "duckdb",
                "db_path": str(tmp / "lora.duckdb"),
                "batch_rows": 256,
                "batch_ms": 1000,
                "checkpoint_interval_s": 300,
            },
        },
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Happy path — sanity check the validator round-trip first so the
# fail-fast tests below are informative.
# ---------------------------------------------------------------------------


def test_minimal_valid_round_trip() -> None:
    typed = load_typed(_minimal_valid())
    assert typed.core.listen == "127.0.0.1:5555"
    assert len(typed.core.upstreams) == 1
    assert typed.core.upstreams[0].name == "radio_868"
    assert typed.core.aggregator.window_ms == 200
    assert typed.core.decoders.enabled == ("meshcore", "lorawan", "raw")
    # Default bridge tables fill in.
    assert typed.bridge_meshcore.listen_port == 7834
    assert typed.bridge_serial.listen_port == 7835


def test_daemon_config_from_legacy_toml_delegates(tmp_path: Path) -> None:
    """``DaemonConfig.from_legacy_toml`` is now a thin wrapper around load_typed."""
    cfg = _minimal_valid()
    daemon_cfg = DaemonConfig.from_legacy_toml(cfg)
    # Listen port is whatever the typed config says.
    assert daemon_cfg.listen_port == 5555
    assert len(daemon_cfg.upstreams) == 1
    assert daemon_cfg.upstreams[0].name == "radio_868"


# ---------------------------------------------------------------------------
# Fail-fast: legacy top-level keys
# ---------------------------------------------------------------------------


def test_legacy_aggregator_top_level_fails_fast() -> None:
    cfg = _minimal_valid()
    cfg["aggregator"] = {"window_ms": 200, "listen": "127.0.0.1:5555"}
    with pytest.raises(TypedConfigError, match=re.compile(r"legacy.*aggregator", re.I)):
        load_typed(cfg)


def test_legacy_aggregator_via_from_legacy_toml() -> None:
    cfg = _minimal_valid()
    cfg["aggregator"] = {"window_ms": 200}
    with pytest.raises(TypedConfigError, match=re.compile(r"legacy.*aggregator", re.I)):
        DaemonConfig.from_legacy_toml(cfg)


def test_legacy_meshcore_top_level_fails_fast() -> None:
    cfg = _minimal_valid()
    cfg["meshcore"] = {"name": "DO2THX", "lat": 53.5, "lon": 9.9}
    with pytest.raises(TypedConfigError, match=re.compile(r"legacy.*meshcore", re.I)):
        load_typed(cfg)


# ---------------------------------------------------------------------------
# Fail-fast: malformed [core.upstream]
# ---------------------------------------------------------------------------


def test_string_form_core_upstream_fails_fast() -> None:
    cfg = _minimal_valid()
    cfg["core"]["upstream"] = "127.0.0.1:5556"
    with pytest.raises(TypedConfigError, match=r"string form not supported"):
        load_typed(cfg)


def test_empty_core_upstream_array_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["upstream"] = []
    with pytest.raises(TypedConfigError, match=r"non-empty array"):
        load_typed(cfg)


def test_duplicate_upstream_name_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["upstream"] = [
        {"name": "radio_868", "host": "127.0.0.1", "port": 5556},
        {"name": "radio_868", "host": "127.0.0.1", "port": 5566},
    ]
    with pytest.raises(TypedConfigError, match=r"duplicates"):
        load_typed(cfg)


def test_upstream_port_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["upstream"] = [
        {"name": "radio_868", "host": "127.0.0.1", "port": 70000},
    ]
    with pytest.raises(TypedConfigError, match=r"port must be 1\.\.65535"):
        load_typed(cfg)


# ---------------------------------------------------------------------------
# Fail-fast: unknown decoder name
# ---------------------------------------------------------------------------


def test_unknown_decoder_name_fails_fast() -> None:
    cfg = _minimal_valid()
    cfg["core"]["decoders"]["enabled"] = ["meshcore", "definitely-not-a-decoder"]
    with pytest.raises(TypedConfigError, match=r"unknown plugin"):
        load_typed(cfg)


def test_empty_enabled_decoders_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["decoders"]["enabled"] = []
    with pytest.raises(TypedConfigError, match=r"non-empty list"):
        load_typed(cfg)


# ---------------------------------------------------------------------------
# Bound violations — spec Section 5 table
# ---------------------------------------------------------------------------


def test_window_ms_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["aggregator"]["window_ms"] = 100_000
    with pytest.raises(TypedConfigError, match=r"window_ms must be 0\.\.10000"):
        load_typed(cfg)


def test_fanout_queue_depth_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["fanout_queue_depth"] = 10
    with pytest.raises(TypedConfigError, match=r"fanout_queue_depth must be 256"):
        load_typed(cfg)


def test_storage_batch_rows_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["storage"]["batch_rows"] = 0
    with pytest.raises(TypedConfigError, match=r"batch_rows must be 1\.\.65536"):
        load_typed(cfg)


def test_storage_batch_ms_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"]["storage"]["batch_ms"] = 0
    with pytest.raises(TypedConfigError, match=r"batch_ms must be 1\.\.60000"):
        load_typed(cfg)


def test_bridge_meshcore_listen_port_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["bridge"] = {"meshcore": {"listen_port": 100_000}}
    with pytest.raises(
        TypedConfigError, match=r"bridge\.meshcore.*listen_port must be 1\.\.65535"
    ):
        load_typed(cfg)


def test_bridge_serial_listen_port_out_of_range_fails() -> None:
    cfg = _minimal_valid()
    cfg["bridge"] = {"serial": {"listen_port": 0}}
    with pytest.raises(
        TypedConfigError, match=r"bridge\.serial.*listen_port must be 1\.\.65535"
    ):
        load_typed(cfg)


# ---------------------------------------------------------------------------
# Missing required sections
# ---------------------------------------------------------------------------


def test_missing_core_section_fails() -> None:
    with pytest.raises(TypedConfigError, match=r"\[core\] section"):
        load_typed({})


def test_missing_core_decoders_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"].pop("decoders")
    with pytest.raises(TypedConfigError, match=r"\[core\.decoders\]"):
        load_typed(cfg)


def test_missing_core_identity_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"].pop("identity")
    with pytest.raises(TypedConfigError, match=r"\[core\.identity\]"):
        load_typed(cfg)


def test_missing_listen_fails() -> None:
    cfg = _minimal_valid()
    cfg["core"].pop("listen")
    with pytest.raises(TypedConfigError, match=r"core.*listen"):
        load_typed(cfg)


# ---------------------------------------------------------------------------
# Optional bridge sections fill defaults
# ---------------------------------------------------------------------------


def test_bridge_sections_optional() -> None:
    """Daemon-only deployments don't need to supply [bridge.*]."""
    typed = load_typed(_minimal_valid())  # no [bridge] in dict
    assert typed.bridge_meshcore.listen_port == 7834
    assert typed.bridge_meshcore.tx_via_core is True
    assert typed.bridge_serial.listen_port == 7835


def test_bridge_meshcore_overrides_propagate() -> None:
    cfg = _minimal_valid()
    cfg["bridge"] = {
        "meshcore": {
            "listen_port": 9999,
            "tx_via_core": False,
            "eeprom_path": "/tmp/eeprom.cbor",
        },
        "serial": {"listen_port": 9998},
    }
    typed = load_typed(cfg)
    assert typed.bridge_meshcore.listen_port == 9999
    assert typed.bridge_meshcore.tx_via_core is False
    assert typed.bridge_meshcore.eeprom_path == Path("/tmp/eeprom.cbor")
    assert typed.bridge_serial.listen_port == 9998
