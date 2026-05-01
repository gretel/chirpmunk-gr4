#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""In-process tests for ``lora.hwtests.cli`` so coverage actually
counts the dispatcher branches."""

from __future__ import annotations

import pytest

from lora.hwtests import cli as hw_cli


class TestHwtestCliInProc:
    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for cmd in (
            "decode",
            "scan",
            "tx",
            "bridge",
            "transmit",
            "bridge-live",
            "scan-perf",
            "trx-perf",
        ):
            assert cmd in out

    def test_subcmd_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["decode", "--help"])
        assert exc.value.code == 0
        assert "--matrix" in capsys.readouterr().out

    def test_transmit_help_no_sdr_flags(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``transmit`` is companion-only — must not advertise SDR flags."""
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["transmit", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--matrix" in out
        assert "--tx-power" in out
        # SDR-only flags must NOT appear.
        for sdr_flag in ("--config", "--binary", "--host"):
            assert sdr_flag not in out, f"{sdr_flag} leaked into transmit --help"

    def test_passthrough_bridge_live_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["bridge-live", "--help"])
        assert exc.value.code == 0
        assert "--bridge" in capsys.readouterr().out

    def test_passthrough_scan_perf_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["scan-perf", "--help"])
        assert exc.value.code == 0
        assert "lora_scan" in capsys.readouterr().out.lower()

    def test_passthrough_trx_perf_help(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            hw_cli.main(["trx-perf", "--help"])
        assert exc.value.code == 0
        assert "lora_trx" in capsys.readouterr().out.lower()
