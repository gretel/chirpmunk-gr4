# SPDX-License-Identifier: ISC
"""Tests for :class:`lora.daemon.config.SupervisorConfig` parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from lora.daemon.config import SupervisorConfig
from lora.daemon.supervisor import KNOWN_SERVICES, service_argv, validate_services


def test_minimal_services_list() -> None:
    cfg = SupervisorConfig.from_toml(
        {"startup": {"services": ["core", "bridge.meshcore", "wav"]}}
    )
    assert cfg.services == ("core", "bridge.meshcore", "wav")


def test_missing_startup_section_raises() -> None:
    with pytest.raises(ValueError, match=r"\[startup\] section"):
        SupervisorConfig.from_toml({})


def test_missing_services_key_raises() -> None:
    with pytest.raises(ValueError, match=r"\[startup\].services"):
        SupervisorConfig.from_toml({"startup": {}})


def test_empty_services_list_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        SupervisorConfig.from_toml({"startup": {"services": []}})


def test_non_list_services_raises() -> None:
    with pytest.raises(ValueError, match="list of strings"):
        SupervisorConfig.from_toml({"startup": {"services": "core"}})


def test_non_string_entry_raises() -> None:
    with pytest.raises(ValueError, match="list of strings"):
        SupervisorConfig.from_toml({"startup": {"services": ["core", 42]}})


def test_service_argv_core() -> None:
    argv = service_argv(
        "core", lora_argv0="/usr/bin/lora", config_path=Path("/etc/lora.toml")
    )
    assert argv == ["/usr/bin/lora", "core", "--config", "/etc/lora.toml"]


def test_service_argv_bridge_meshcore() -> None:
    argv = service_argv(
        "bridge.meshcore", lora_argv0="lora", config_path=Path("c.toml")
    )
    assert argv == ["lora", "bridge", "meshcore", "--config", "c.toml"]


def test_service_argv_wav_omits_config_flag() -> None:
    """``lora wav`` doesn't accept ``--config`` — supervisor must skip it."""
    argv = service_argv("wav", lora_argv0="lora", config_path=Path("c.toml"))
    assert argv == ["lora", "wav"]


def test_service_argv_bridge_serial_omits_config_flag() -> None:
    """``lora bridge serial`` doesn't accept ``--config`` either."""
    argv = service_argv("bridge.serial", lora_argv0="lora", config_path=Path("c.toml"))
    assert argv == ["lora", "bridge", "serial"]


def test_known_services_excludes_interactive_and_oneshot() -> None:
    # Visualisation cmds and one-shots must NOT be supervisable.
    for name in ("mon", "waterfall", "spectrum", "tx", "hwtest"):
        assert name not in KNOWN_SERVICES, f"{name} must not appear in KNOWN_SERVICES"


def test_validate_services_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown service 'mon'"):
        validate_services(("core", "mon"))


def test_validate_services_passes_for_all_known() -> None:
    validate_services(tuple(KNOWN_SERVICES.keys()))  # must not raise
