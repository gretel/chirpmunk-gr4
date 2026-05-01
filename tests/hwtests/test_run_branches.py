#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Coverage for the early-exit branches of the per-mode ``run()`` entries.

These tests don't bring up real hardware — they hit the argument-validation
+ companion-not-responding paths and, where possible, the
``parse_tcp``/``_is_tuning_scan_config`` helpers.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import lora.hwtests.bridge_test as bridge_test
import lora.hwtests.decode_test as decode_test
import lora.hwtests.scan_test as scan_test
import lora.hwtests.transmit_test as transmit_test
import lora.hwtests.tx_test as tx_test
from lora.hwtests.scan_test import _is_tuning_scan_config


class TestParseTcp:
    def test_returns_none_for_falsy(self) -> None:
        assert decode_test.parse_tcp(None) == (None, None)
        assert decode_test.parse_tcp("") == (None, None)

    def test_parses_host_port(self) -> None:
        host, port = decode_test.parse_tcp("10.0.0.5:7835")
        assert host == "10.0.0.5"
        assert port == 7835

    def test_transmit_parse_tcp_falsy(self) -> None:
        assert transmit_test.parse_tcp(None) == (None, None)
        assert transmit_test.parse_tcp("") == (None, None)

    def test_transmit_parse_tcp_host_port(self) -> None:
        host, port = transmit_test.parse_tcp("10.0.0.5:7835")
        assert host == "10.0.0.5"
        assert port == 7835


class TestIsTuningScanConfig:
    def test_streaming_true_returns_false(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text("[scan]\nstreaming = true\n")
        assert _is_tuning_scan_config(str(cfg)) is False

    def test_streaming_false_returns_true(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text("[scan]\nstreaming = false\n")
        assert _is_tuning_scan_config(str(cfg)) is True

    def test_missing_section_defaults_to_streaming(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text("# empty\n")
        assert _is_tuning_scan_config(str(cfg)) is False

    def test_missing_file_returns_false(self) -> None:
        assert _is_tuning_scan_config("/nonexistent/path.toml") is False

    def test_malformed_toml_returns_false(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad.toml"
        cfg.write_text("][not toml")
        assert _is_tuning_scan_config(str(cfg)) is False
        # Sanity: confirm tomllib actually rejects this so the test
        # exercises the except-branch and not a happy path.
        try:
            tomllib.loads("][not toml")
            raise AssertionError("expected TOMLDecodeError")
        except tomllib.TOMLDecodeError:
            pass


class TestRunMissingConnection:
    def test_decode_missing_serial_and_tcp_returns_2(self) -> None:
        rc = decode_test.run(
            serial_port=None,
            tcp_host=None,
            tcp_port=None,
            matrix_name="basic",
            attach=True,
        )
        assert rc == 2

    def test_scan_missing_serial_and_tcp_returns_2(self) -> None:
        rc = scan_test.run(
            serial_port=None,
            tcp_host=None,
            tcp_port=None,
            matrix_name="basic",
            attach=True,
        )
        assert rc == 2

    def test_transmit_missing_serial_and_tcp_returns_2(self) -> None:
        rc = transmit_test.run(
            serial_port=None,
            tcp_host=None,
            tcp_port=None,
            matrix_name="basic",
            attach=True,
        )
        assert rc == 2


class TestBridgePayloadStability:
    """Bridge mode has a few additional non-async helpers worth pinning."""

    def test_bridge_payload_distinct_for_distinct_points(self) -> None:
        from lora.hwtests.matrix import ConfigPoint

        a = ConfigPoint(sf=7, bw=62500, freq_mhz=869.618)
        b = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        # 256 samples → expect at least 8 distinct nonces per point and
        # the SF prefix differs.
        a_set = {bridge_test.bridge_payload(a) for _ in range(256)}
        b_set = {bridge_test.bridge_payload(b) for _ in range(256)}
        assert all(p.startswith("sf7bw62") for p in a_set)
        assert all(p.startswith("sf8bw62") for p in b_set)
        assert a_set.isdisjoint(b_set)


class TestTxHelpersImportability:
    def test_get_sdr_pubkey_module_import(self) -> None:
        # Just make sure the module imports cleanly under coverage —
        # anything more requires a UDP listener (covered by
        # test_tx_helpers).
        assert callable(tx_test.get_sdr_pubkey)
        assert callable(tx_test.sdr_tx_advert)
        assert callable(tx_test.sdr_tx_msg)
