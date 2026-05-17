#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""TOML test-config schema for the ``lora hwtest`` harness.

A test-config file fully describes one experiment: which reference
companion to drive, which matrix to sweep, and which device(s) under
test (DUTs) to spawn and observe. One DUT is the legacy single-SDR
mode; two or more DUTs put the harness into "compete" mode where the
same reference TX is decoded by every DUT in parallel and results are
matched per-frame via ``payload_hash``.

Schema (all fields shown):

    [test]
    label      = "b210-vs-pluto"
    hypothesis = "Compare B210 UHD vs Pluto IIO direct on identical RF"

    [reference]
    tcp = "heltec.local:5000"
    # OR: serial = "/dev/cu.usbserial-0001"

    [matrix]
    name = "sf_sweep"

    [[dut]]
    label       = "b210"
    binary      = "./build/apps/lora_trx"
    config_file = "apps/config.toml"
    host        = "127.0.0.1"
    port        = 5556

    [[dut]]
    label       = "pluto"
    binary      = "./build/apps/lora_trx"
    config_file = "apps/config-pluto.toml"
    host        = "127.0.0.1"
    port        = 5566

The DUT ``binary`` may also be a different transceiver entirely (e.g.
``cargo run --release --bin chirpmunk-trx --``) — the harness only
relies on the shared CBOR ``lora_frame`` schema on the subscribed UDP
port.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DutConfig:
    """One device-under-test entry. Each DUT gets its own SDR binary,
    config file, and UDP port."""

    label: str
    binary: str
    config_file: str
    host: str = "127.0.0.1"
    port: int = 5556


@dataclass(frozen=True)
class ReferenceConfig:
    """The companion (reference) device — Semtech-chip MeshCore node.

    Exactly one of ``tcp`` or ``serial`` must be set.
    """

    tcp: str | None = None
    serial: str | None = None

    def __post_init__(self) -> None:
        if (self.tcp is None) == (self.serial is None):
            raise ValueError("[reference] must set exactly one of tcp / serial")


@dataclass(frozen=True)
class MatrixConfig:
    """Reference to a named matrix in :data:`lora.hwtests.matrix.MATRICES`."""

    name: str


@dataclass(frozen=True)
class TestConfig:
    """Top-level test-config document."""

    # Tell pytest this dataclass is not a test class (it would otherwise
    # warn on collection because the name begins with ``Test``).
    __test__ = False

    label: str
    hypothesis: str
    matrix: MatrixConfig
    duts: tuple[DutConfig, ...]
    reference: ReferenceConfig | None = None
    source_path: str = ""

    def __post_init__(self) -> None:
        if not self.duts:
            raise ValueError("[[dut]]: at least one DUT required")
        labels = [d.label for d in self.duts]
        if len(labels) != len(set(labels)):
            raise ValueError(f"[[dut]]: duplicate labels: {labels}")
        endpoints = [(d.host, d.port) for d in self.duts]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError(f"[[dut]]: duplicate (host, port) pairs: {endpoints}")


def _require(d: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in d:
        raise ValueError(f"{ctx}: missing required key '{key}'")
    return d[key]


def load_test_config(path: str | Path) -> TestConfig:
    """Parse a TOML test-config file. Validates DUT label/port uniqueness."""
    p = Path(path)
    raw = tomllib.loads(p.read_text(encoding="utf-8"))

    test = raw.get("test", {})
    label = str(test.get("label", p.stem))
    hypothesis = str(test.get("hypothesis", ""))

    ref_raw = raw.get("reference")
    reference: ReferenceConfig | None
    if ref_raw is not None:
        reference = ReferenceConfig(
            tcp=ref_raw.get("tcp"),
            serial=ref_raw.get("serial"),
        )
    else:
        reference = None

    matrix_raw = raw.get("matrix", {})
    matrix = MatrixConfig(name=str(_require(matrix_raw, "name", "[matrix]")))

    duts_raw = raw.get("dut", [])
    if not isinstance(duts_raw, list):
        raise ValueError("[[dut]] must be an array of tables")
    duts = tuple(
        DutConfig(
            label=str(_require(d, "label", "[[dut]]")),
            binary=str(_require(d, "binary", "[[dut]]")),
            config_file=str(_require(d, "config_file", "[[dut]]")),
            host=str(d.get("host", "127.0.0.1")),
            port=int(_require(d, "port", "[[dut]]")),
        )
        for d in duts_raw
    )

    return TestConfig(
        label=label,
        hypothesis=hypothesis,
        matrix=matrix,
        duts=duts,
        reference=reference,
        source_path=str(p),
    )


def synthesize_legacy(
    *,
    serial_port: str | None,
    tcp_host: str | None,
    tcp_port: int | None,
    matrix_name: str,
    config_file: str,
    binary: str,
    label: str,
    hypothesis: str,
    host: str,
    udp_port: int,
    dut_label: str = "default",
) -> TestConfig:
    """Build a 1-DUT TestConfig from legacy CLI flags. Backward compat.

    Used when ``--test-config`` is absent and the user passes the old
    ``--config`` / ``--binary`` / ``--tcp`` / ``--serial`` / ``--matrix``
    flags.
    """
    reference: ReferenceConfig | None = None
    if tcp_host and tcp_port:
        reference = ReferenceConfig(tcp=f"{tcp_host}:{tcp_port}")
    elif serial_port:
        reference = ReferenceConfig(serial=serial_port)
    return TestConfig(
        label=label or f"{matrix_name}",
        hypothesis=hypothesis,
        matrix=MatrixConfig(name=matrix_name),
        duts=(
            DutConfig(
                label=dut_label,
                binary=binary or "./build/apps/lora_trx",
                config_file=config_file,
                host=host,
                port=udp_port,
            ),
        ),
        reference=reference,
    )


__all__ = [
    "DutConfig",
    "MatrixConfig",
    "ReferenceConfig",
    "TestConfig",
    "load_test_config",
    "synthesize_legacy",
]
