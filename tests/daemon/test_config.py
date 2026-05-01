# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.daemon.config` projection of typed schema.

Phase 2G: ``DaemonConfig.from_legacy_toml`` is a thin wrapper around
:func:`lora.core.config_typed.load_typed` plus
:meth:`DaemonConfig.from_typed`. The "strict no-compat" path —
rejecting legacy ``[aggregator]`` / ``[meshcore]`` keys etc — lives
in ``test_legacy_keys_fail_fast.py``. This module focuses on the
projection from typed config onto the runtime data class.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from lora.daemon.config import DaemonConfig, UpstreamConfig


def _minimal_valid() -> dict[str, Any]:
    """Build a minimal valid Phase 2G TOML dict."""
    tmp = Path(tempfile.mkdtemp(prefix="lora-cfg-"))
    return {
        "core": {
            "listen": "127.0.0.1:5555",
            "fanout_queue_depth": 10000,
            "upstream": [
                {"name": "radio_868", "host": "127.0.0.1", "port": 5556},
            ],
            "aggregator": {"window_ms": 200, "max_candidates": 8},
            "decoders": {"enabled": ["meshcore", "raw"]},
            "identity": {
                "identity_file": str(tmp / "identity.bin"),
                "keys_dir": str(tmp / "keys"),
                "channels_dir": str(tmp / "channels"),
                "contacts_dir": str(tmp / "contacts"),
            },
            "storage": {
                "backend": "duckdb",
                "db_path": str(tmp / "lora.duckdb"),
            },
        },
    }


def test_minimal_round_trip() -> None:
    cfg = DaemonConfig.from_legacy_toml(_minimal_valid())
    assert cfg.listen_host == "127.0.0.1"
    # Listen port reflects the typed config verbatim.
    assert cfg.listen_port == 5555
    assert cfg.aggregator_window_ms == 200
    assert len(cfg.upstreams) == 1
    up = cfg.upstreams[0]
    assert up.name == "radio_868"
    assert up.host == "127.0.0.1"
    assert up.port == 5556
    assert up.strict is True


def test_multi_upstream() -> None:
    cfg_dict = _minimal_valid()
    cfg_dict["core"]["upstream"] = [
        {"name": "radio_868", "host": "127.0.0.1", "port": 5556},
        {"name": "radio_2g4", "host": "127.0.0.1", "port": 5566},
    ]
    cfg = DaemonConfig.from_legacy_toml(cfg_dict)
    names = [u.name for u in cfg.upstreams]
    assert names == ["radio_868", "radio_2g4"]
    assert cfg.upstreams[1].port == 5566


def test_decoder_options_propagate() -> None:
    cfg_dict = _minimal_valid()
    cfg_dict["core"]["decoders"]["meshcore"] = {"decrypt": False}
    cfg = DaemonConfig.from_legacy_toml(cfg_dict)
    assert "meshcore" in cfg.decoders_options
    assert cfg.decoders_options["meshcore"]["decrypt"] is False


def test_logging_overrides() -> None:
    cfg_dict = _minimal_valid()
    cfg_dict["logging"] = {"level": "DEBUG", "color": False}
    cfg = DaemonConfig.from_legacy_toml(cfg_dict)
    assert cfg.log_level == "DEBUG"
    assert cfg.log_color is False


def test_upstream_config_strict_default_true() -> None:
    up = UpstreamConfig(name="x", host="127.0.0.1", port=5556)
    assert up.strict is True
