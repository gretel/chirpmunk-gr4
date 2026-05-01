# SPDX-License-Identifier: ISC
"""Smoke test for the lora CLI dispatcher.

Phase 0: ensures the dispatcher loads, --help lists every subcommand, and
each stub exits with status 2 and a 'phase pending' message on stderr.
"""

from __future__ import annotations

import subprocess
import sys

EXPECTED_TOP_LEVEL = {
    "core",
    "mon",
    "waterfall",
    "spectrum",
    "wav",
    "tx",
    "bridge",
    "hwtest",
}

EXPECTED_BRIDGE_KINDS = {"meshcore", "serial"}


def _run_lora(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "lora.cli", *args],
        capture_output=True,
        text=True,
    )


def test_lora_help_lists_all_subcommands() -> None:
    result = _run_lora("--help")
    assert result.returncode == 0, (
        f"--help exit code {result.returncode}; stderr={result.stderr!r}"
    )
    out = result.stdout + result.stderr
    missing = {s for s in EXPECTED_TOP_LEVEL if s not in out}
    assert not missing, f"missing subcommands in --help: {missing}"


def test_lora_bridge_help_lists_kinds() -> None:
    result = _run_lora("bridge", "--help")
    assert result.returncode == 0, (
        f"bridge --help exit code {result.returncode}; stderr={result.stderr!r}"
    )
    out = result.stdout + result.stderr
    missing = {k for k in EXPECTED_BRIDGE_KINDS if k not in out}
    assert not missing, f"missing bridge kinds in --help: {missing}"


def test_lora_no_remaining_stubs() -> None:
    """Phase 5 wired hwtest — no top-level subcommand should still
    print the legacy 'phase pending' stub message."""
    for cmd in ("hwtest",):
        result = _run_lora(cmd, "--help")
        assert result.returncode == 0, (
            f"{cmd!r} --help expected exit 0, got {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
        out = result.stdout + result.stderr
        assert "phase pending" not in out.lower(), f"{cmd!r} still stubbed: {out!r}"


def test_lora_phase3_subcmds_help_works() -> None:
    """Phase 3 subcommands must accept --help and exit 0 (not 'phase pending')."""
    for cmd in ("mon", "waterfall", "spectrum", "wav", "tx"):
        result = _run_lora(cmd, "--help")
        assert result.returncode == 0, (
            f"{cmd!r} --help expected exit 0, got {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
        out = result.stdout + result.stderr
        assert "phase pending" not in out.lower(), (
            f"{cmd!r} --help still says 'phase pending': {out!r}"
        )


def test_lora_bridge_meshcore_help_works() -> None:
    """Phase 4 wired ``lora bridge meshcore`` — ``--help`` must exit 0."""
    result = _run_lora("bridge", "meshcore", "--help")
    assert result.returncode == 0, (
        f"bridge meshcore --help exit {result.returncode}; stderr={result.stderr!r}"
    )
    out = result.stdout + result.stderr
    assert "phase pending" not in out.lower()
    assert "MeshCore companion" in out


def test_lora_bridge_serial_help_works() -> None:
    """Phase 4 wired ``lora bridge serial`` — ``--help`` must exit 0."""
    result = _run_lora("bridge", "serial", "--help")
    assert result.returncode == 0, (
        f"bridge serial --help exit {result.returncode}; stderr={result.stderr!r}"
    )
    out = result.stdout + result.stderr
    assert "phase pending" not in out.lower()
    assert "DTR-safe" in out or "serial" in out.lower()


def test_lora_bridge_meshcore_migrate_json_help_works() -> None:
    """Phase 4 wired migrate-json — ``--help`` must exit 0."""
    result = _run_lora("bridge", "meshcore", "migrate-json", "--help")
    assert result.returncode == 0
    out = result.stdout + result.stderr
    assert "config.json" in out


def test_lora_no_args_errors() -> None:
    result = _run_lora()
    assert result.returncode != 0, "no args should error"
