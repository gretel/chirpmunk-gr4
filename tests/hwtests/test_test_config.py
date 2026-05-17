#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.test_config``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lora.hwtests.test_config import (
    DutConfig,
    MatrixConfig,
    ReferenceConfig,
    TestConfig,
    load_test_config,
    synthesize_legacy,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "test.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


class TestDutConfig:
    def test_defaults(self) -> None:
        d = DutConfig(label="a", binary="x", config_file="y")
        assert d.host == "127.0.0.1"
        assert d.port == 5556

    def test_explicit_fields(self) -> None:
        d = DutConfig(
            label="pluto", binary="bin", config_file="cfg", host="10.0.0.1", port=5566
        )
        assert d.label == "pluto"
        assert d.host == "10.0.0.1"
        assert d.port == 5566


class TestReferenceConfig:
    def test_tcp_only(self) -> None:
        r = ReferenceConfig(tcp="host:5000")
        assert r.tcp == "host:5000"
        assert r.serial is None

    def test_serial_only(self) -> None:
        r = ReferenceConfig(serial="/dev/ttyUSB0")
        assert r.serial == "/dev/ttyUSB0"
        assert r.tcp is None

    def test_both_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ReferenceConfig(tcp="h:1", serial="/dev/x")

    def test_neither_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ReferenceConfig()


class TestTestConfigValidation:
    def _ok_dut(self, label: str = "a", port: int = 5556) -> DutConfig:
        return DutConfig(label=label, binary="b", config_file="c", port=port)

    def test_at_least_one_dut(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            TestConfig(label="t", hypothesis="", matrix=MatrixConfig("basic"), duts=())

    def test_duplicate_labels_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate labels"):
            TestConfig(
                label="t",
                hypothesis="",
                matrix=MatrixConfig("basic"),
                duts=(self._ok_dut("a", 5556), self._ok_dut("a", 5566)),
            )

    def test_duplicate_endpoints_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            TestConfig(
                label="t",
                hypothesis="",
                matrix=MatrixConfig("basic"),
                duts=(self._ok_dut("a", 5556), self._ok_dut("b", 5556)),
            )

    def test_two_duts_ok(self) -> None:
        tc = TestConfig(
            label="t",
            hypothesis="",
            matrix=MatrixConfig("basic"),
            duts=(self._ok_dut("a", 5556), self._ok_dut("b", 5566)),
        )
        assert len(tc.duts) == 2


class TestLoadTestConfig:
    def test_full_dual_dut(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "x-vs-y"
            hypothesis = "compare"

            [reference]
            tcp = "host:5000"

            [matrix]
            name = "sf_sweep"

            [[dut]]
            label = "x"
            binary = "./bin1"
            config_file = "cfg1.toml"
            host = "127.0.0.1"
            port = 5556

            [[dut]]
            label = "y"
            binary = "./bin2"
            config_file = "cfg2.toml"
            port = 5566
            """,
        )
        tc = load_test_config(p)
        assert tc.label == "x-vs-y"
        assert tc.hypothesis == "compare"
        assert tc.reference is not None and tc.reference.tcp == "host:5000"
        assert tc.matrix.name == "sf_sweep"
        assert len(tc.duts) == 2
        assert tc.duts[0].label == "x"
        assert tc.duts[0].port == 5556
        assert tc.duts[1].label == "y"
        assert tc.duts[1].host == "127.0.0.1"  # default
        assert tc.duts[1].port == 5566

    def test_serial_reference(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "a"

            [reference]
            serial = "/dev/cu.usbserial-0001"

            [matrix]
            name = "basic"

            [[dut]]
            label = "a"
            binary = "b"
            config_file = "c"
            port = 5556
            """,
        )
        tc = load_test_config(p)
        assert tc.reference is not None
        assert tc.reference.serial == "/dev/cu.usbserial-0001"
        assert tc.reference.tcp is None

    def test_no_reference_section(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "a"

            [matrix]
            name = "basic"

            [[dut]]
            label = "a"
            binary = "b"
            config_file = "c"
            port = 5556
            """,
        )
        tc = load_test_config(p)
        assert tc.reference is None

    def test_label_defaults_to_filename_stem(self, tmp_path: Path) -> None:
        p = tmp_path / "myrun.toml"
        p.write_text(
            textwrap.dedent(
                """
                [matrix]
                name = "basic"

                [[dut]]
                label = "a"
                binary = "b"
                config_file = "c"
                port = 5556
                """
            ),
            encoding="utf-8",
        )
        tc = load_test_config(p)
        assert tc.label == "myrun"

    def test_missing_matrix_name(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "a"

            [matrix]

            [[dut]]
            label = "a"
            binary = "b"
            config_file = "c"
            port = 5556
            """,
        )
        with pytest.raises(ValueError, match="missing required key 'name'"):
            load_test_config(p)

    def test_missing_dut_field(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "a"

            [matrix]
            name = "basic"

            [[dut]]
            label = "a"
            binary = "b"
            port = 5556
            """,
        )
        with pytest.raises(ValueError, match="config_file"):
            load_test_config(p)

    def test_duplicate_ports_in_toml(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            [test]
            label = "a"

            [matrix]
            name = "basic"

            [[dut]]
            label = "a"
            binary = "b"
            config_file = "c"
            port = 5556

            [[dut]]
            label = "b"
            binary = "b"
            config_file = "c"
            port = 5556
            """,
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_test_config(p)


class TestSynthesizeLegacy:
    def test_tcp_companion(self) -> None:
        tc = synthesize_legacy(
            serial_port=None,
            tcp_host="heltec.local",
            tcp_port=5000,
            matrix_name="sf_sweep",
            config_file="apps/config.toml",
            binary="",
            label="run-1",
            hypothesis="baseline",
            host="127.0.0.1",
            udp_port=5556,
        )
        assert tc.label == "run-1"
        assert tc.hypothesis == "baseline"
        assert tc.matrix.name == "sf_sweep"
        assert tc.reference is not None
        assert tc.reference.tcp == "heltec.local:5000"
        assert len(tc.duts) == 1
        assert tc.duts[0].label == "default"
        assert tc.duts[0].config_file == "apps/config.toml"
        assert tc.duts[0].binary == "./build/apps/lora_trx"
        assert tc.duts[0].port == 5556

    def test_serial_companion(self) -> None:
        tc = synthesize_legacy(
            serial_port="/dev/cu.usbserial-0001",
            tcp_host=None,
            tcp_port=None,
            matrix_name="basic",
            config_file="apps/config.toml",
            binary="",
            label="",
            hypothesis="",
            host="127.0.0.1",
            udp_port=5556,
        )
        assert tc.reference is not None
        assert tc.reference.serial == "/dev/cu.usbserial-0001"

    def test_no_companion_yields_none_reference(self) -> None:
        tc = synthesize_legacy(
            serial_port=None,
            tcp_host=None,
            tcp_port=None,
            matrix_name="basic",
            config_file="apps/config.toml",
            binary="",
            label="",
            hypothesis="",
            host="127.0.0.1",
            udp_port=5556,
        )
        assert tc.reference is None

    def test_explicit_binary_used_verbatim(self) -> None:
        tc = synthesize_legacy(
            serial_port=None,
            tcp_host="h",
            tcp_port=1,
            matrix_name="basic",
            config_file="cfg",
            binary="./build/apps/lora_trx_v2",
            label="",
            hypothesis="",
            host="127.0.0.1",
            udp_port=5556,
        )
        assert tc.duts[0].binary == "./build/apps/lora_trx_v2"
