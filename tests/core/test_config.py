# SPDX-License-Identifier: ISC
"""Tests for :mod:`lora.core.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from lora.core.config import (
    config_agg_listen,
    config_agg_upstream,
    config_agg_window_ms,
    config_region_scope,
    config_udp_host,
    config_udp_port,
    find_config,
    load_config,
)


def test_find_config_explicit_path_not_found() -> None:
    assert find_config("/nonexistent/path/to/config.toml") is None


def test_find_config_explicit_path_found(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    result = find_config(str(cfg))
    assert result == cfg


def test_load_config_explicit_path_not_found(tmp_path: Path) -> None:
    result = load_config(str(tmp_path / "nope.toml"))
    assert result == {}


def test_load_config_no_config_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = load_config()
    assert result == {}


def test_config_udp_host_defaults() -> None:
    assert config_udp_host({}) == "127.0.0.1"


def test_config_udp_host_from_network_section() -> None:
    cfg = {"network": {"udp_listen": "0.0.0.0"}}
    assert config_udp_host(cfg) == "0.0.0.0"


def test_config_udp_host_top_level_fallback() -> None:
    cfg = {"udp_listen": "10.0.0.1"}
    assert config_udp_host(cfg) == "10.0.0.1"


def test_config_udp_port_default() -> None:
    assert config_udp_port({}) == 5555


def test_config_udp_port_from_network_section() -> None:
    cfg = {"network": {"udp_port": 5566}}
    assert config_udp_port(cfg) == 5566


def test_config_udp_port_top_level_fallback() -> None:
    cfg = {"udp_port": 7777}
    assert config_udp_port(cfg) == 7777


def test_config_agg_upstream_default() -> None:
    assert config_agg_upstream({}) == "127.0.0.1:5556"


def test_config_agg_upstream_from_section() -> None:
    cfg = {"aggregator": {"upstream": "10.0.0.1:5566"}}
    assert config_agg_upstream(cfg) == "10.0.0.1:5566"


def test_config_agg_upstream_top_level_fallback() -> None:
    cfg = {"agg_upstream": "10.0.0.1:7777"}
    assert config_agg_upstream(cfg) == "10.0.0.1:7777"


def test_config_agg_listen_default() -> None:
    assert config_agg_listen({}) == "127.0.0.1:5555"


def test_config_agg_listen_from_section() -> None:
    cfg = {"aggregator": {"listen": "0.0.0.0:5555"}}
    assert config_agg_listen(cfg) == "0.0.0.0:5555"


def test_config_agg_listen_top_level_fallback() -> None:
    cfg = {"agg_listen": "127.0.0.1:7777"}
    assert config_agg_listen(cfg) == "127.0.0.1:7777"


def test_config_agg_window_ms_default() -> None:
    assert config_agg_window_ms({}) == 200


def test_config_agg_window_ms_from_section() -> None:
    cfg = {"aggregator": {"window_ms": 500}}
    assert config_agg_window_ms(cfg) == 500


def test_config_agg_window_ms_top_level_fallback() -> None:
    cfg = {"agg_window_ms": 1000}
    assert config_agg_window_ms(cfg) == 1000


def test_config_region_scope_default() -> None:
    assert config_region_scope({}) == ""


def test_config_region_scope_from_meshcore() -> None:
    cfg = {"meshcore": {"region_scope": "EU868"}}
    assert config_region_scope(cfg) == "EU868"
