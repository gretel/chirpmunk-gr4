#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.matrix`` — pure data + builders, no I/O."""

from __future__ import annotations

import pytest

from lora.hwtests.matrix import (
    FREQ_MHZ,
    MATRICES,
    ConfigPoint,
    lora_airtime_s,
    point_label,
)


class TestConfigPoint:
    def test_required_fields(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        assert p.sf == 8
        assert p.bw == 62500
        assert p.freq_mhz == pytest.approx(869.618)
        assert p.tx_power == 2  # default
        assert p.cr == 8  # default

    def test_overrides(self) -> None:
        p = ConfigPoint(sf=11, bw=250000, freq_mhz=869.618, tx_power=10, cr=5)
        assert p.tx_power == 10
        assert p.cr == 5


class TestMatrices:
    @pytest.mark.parametrize(
        "name",
        ["basic", "sf_sweep", "full", "bridge_full", "meshcore_presets", "baseline"],
    )
    def test_matrix_present(self, name: str) -> None:
        assert name in MATRICES
        assert len(MATRICES[name]) > 0
        for p in MATRICES[name]:
            assert isinstance(p, ConfigPoint)

    def test_all_matrices_lock_to_freq_mhz(self) -> None:
        for name, points in MATRICES.items():
            for p in points:
                assert p.freq_mhz == FREQ_MHZ, (
                    f"{name}: ConfigPoint at SF{p.sf}/BW{p.bw} has "
                    f"freq_mhz={p.freq_mhz} != {FREQ_MHZ}"
                )

    def test_basic_matrix_has_three_points(self) -> None:
        assert len(MATRICES["basic"]) == 3

    def test_sf_sweep_covers_sf7_to_12(self) -> None:
        sfs = sorted(p.sf for p in MATRICES["sf_sweep"])
        assert sfs == [7, 8, 9, 10, 11, 12]

    def test_meshcore_presets_use_tx_power_10(self) -> None:
        for p in MATRICES["meshcore_presets"]:
            assert p.tx_power == 10


class TestLoraAirtime:
    def test_sf8_bw62500_typical_advert(self) -> None:
        # SF8/BW62.5k/126B — sanity check against MeshCore EU preset.
        # AN1200.13 yields ~1.14 s for this combo.
        t = lora_airtime_s(sf=8, bw=62500)
        assert 1.0 < t < 1.3

    def test_sf12_bw62500_long_airtime(self) -> None:
        # SF12/BW62.5k/126B — longest airtime in the matrix; roughly 17 s.
        t = lora_airtime_s(sf=12, bw=62500)
        assert 14.0 < t < 20.0

    def test_higher_sf_means_longer_airtime(self) -> None:
        ts = [lora_airtime_s(sf=sf, bw=62500) for sf in (7, 8, 9, 10, 11, 12)]
        assert ts == sorted(ts), "airtime must monotonically increase with SF"

    def test_smaller_payload_means_shorter_airtime(self) -> None:
        long_t = lora_airtime_s(sf=8, bw=62500, n_bytes=126)
        short_t = lora_airtime_s(sf=8, bw=62500, n_bytes=40)
        assert short_t < long_t


class TestPointLabel:
    def test_default_label(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618)
        # Format: "SF{sf}/BW{bw_k:g}k@{freq_mhz:.3f} {tx_power}dBm"
        assert point_label(p) == "SF8/BW62.5k@869.618 2dBm"

    def test_label_includes_cr_when_non_default(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618, cr=5)
        assert point_label(p) == "SF8/BW62.5k@869.618 2dBm CR5"

    def test_label_omits_cr_when_default_8(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618, cr=8)
        assert "CR" not in point_label(p)

    def test_label_includes_tx_power(self) -> None:
        p = ConfigPoint(sf=8, bw=62500, freq_mhz=869.618, tx_power=10)
        assert "10dBm" in point_label(p)
