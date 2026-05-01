#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Tests for ``lora.hwtests.report`` — result dataclass + JSON output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lora.hwtests.report import (
    PointResult,
    build_summary,
    write_results,
)


def _decode_result(
    crc_ok: int = 1, crc_fail: int = 0, best_snr: float | None = 12.3
) -> PointResult:
    r = PointResult(
        config={"sf": 8, "bw": 62500, "freq_mhz": 869.618, "tx_power": 2, "cr": 8}
    )
    r.tx_ok = True
    r.crc_ok = crc_ok
    r.crc_fail = crc_fail
    r.best_snr = best_snr
    if crc_ok > 0:
        r.frames.append(
            {
                "sf": 8,
                "bw": 62500,
                "crc_ok": True,
                "snr_db": best_snr,
                "channel_freq": 869618000.0,
            }
        )
    return r


def _scan_result(det_count: int = 1, best_ratio: float | None = 50.5) -> PointResult:
    r = PointResult(
        config={"sf": 8, "bw": 62500, "freq_mhz": 869.618, "tx_power": 2, "cr": 8}
    )
    r.tx_ok = True
    r.det_count = det_count
    r.raw_det_count = det_count
    r.best_ratio = best_ratio
    r.sweeps = 14
    r.avg_sweep_ms = 555
    return r


class TestPointResult:
    def test_default_factories_are_independent(self) -> None:
        a = PointResult(config={})
        b = PointResult(config={})
        a.frames.append({"x": 1})
        a.detected_sfs[8] = 1
        a.detections.append({"y": 2})
        a.detection_clusters.append({"z": 3})
        assert b.frames == []
        assert b.detected_sfs == {}
        assert b.detections == []
        assert b.detection_clusters == []

    def test_default_zero_counters(self) -> None:
        r = PointResult(config={})
        assert r.crc_ok == 0
        assert r.crc_fail == 0
        assert r.det_count == 0
        assert r.raw_det_count == 0
        assert r.sweeps == 0
        assert r.avg_sweep_ms == 0
        assert r.overflows == 0
        assert r.tx_ok is False
        assert r.best_snr is None
        assert r.best_ratio is None


class TestBuildSummaryDecode:
    def test_summary_aggregates_counts(self) -> None:
        rs = [
            _decode_result(crc_ok=2, crc_fail=1, best_snr=12.3),
            _decode_result(crc_ok=1, crc_fail=0, best_snr=10.0),
            _decode_result(crc_ok=0, crc_fail=0, best_snr=None),
        ]
        # Bare-frame count for 0/0 result
        rs[2].frames = []
        s = build_summary("decode", rs)
        assert s["points"] == 3
        assert s["tx_ok"] == 3
        assert s["total_crc_ok"] == 3
        assert s["total_crc_fail"] == 1
        assert s["points_with_decode"] == 2
        assert s["snr_min"] == 10.0
        assert s["snr_max"] == 12.3

    def test_crc_ok_rate_zero_when_no_frames(self) -> None:
        # Confirm the rate is 0 when total frames = 0.
        s = build_summary("decode", [PointResult(config={})])
        assert s["crc_ok_rate"] == 0


class TestBuildSummaryScan:
    def test_summary_aggregates_detections(self) -> None:
        rs = [
            _scan_result(det_count=2, best_ratio=40.0),
            _scan_result(det_count=0, best_ratio=None),
            _scan_result(det_count=3, best_ratio=60.5),
        ]
        s = build_summary("scan", rs)
        assert s["points"] == 3
        assert s["tx_ok"] == 3
        assert s["total_detections"] == 5
        assert s["points_detected"] == 2
        assert s["ratio_min"] == 40.0
        assert s["ratio_max"] == 60.5


class TestBuildSummaryTx:
    def test_summary_aggregates_pass_rate(self) -> None:
        a = PointResult(config={})
        a.tx_ok = True
        a.crc_ok = 2
        a.crc_fail = 0
        b = PointResult(config={})
        b.tx_ok = True
        b.crc_ok = 1
        b.crc_fail = 1
        s = build_summary("tx", [a, b])
        assert s["points"] == 2
        assert s["tx_ok"] == 2
        assert s["total_tests"] == 4
        assert s["total_pass"] == 3
        assert s["total_fail"] == 1
        assert s["pass_rate"] == 0.75


class TestWriteResults:
    def test_writes_valid_json_with_provenance(self, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        rs = [_decode_result()]
        write_results(
            output_path=str(out),
            label="t1",
            hypothesis="testing",
            mode="decode",
            binary="./build/apps/lora_trx",
            config_file="apps/config.toml",
            matrix_name="basic",
            results=rs,
        )
        doc = json.loads(out.read_text())
        assert doc["experiment"] == "t1"
        assert doc["hypothesis"] == "testing"
        assert doc["mode"] == "decode"
        assert doc["binary"] == "./build/apps/lora_trx"
        assert doc["config_file"] == "apps/config.toml"
        assert doc["matrix"] == "basic"
        assert doc["git_sha"]  # never empty (worst case "unknown")
        assert "timestamp" in doc
        assert "summary" in doc
        assert isinstance(doc["results"], list)
        assert len(doc["results"]) == 1
        assert doc["results"][0]["crc_ok"] == 1

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "deep" / "out.json"
        write_results(
            output_path=str(out),
            label="t",
            hypothesis="",
            mode="scan",
            binary="./build/apps/lora_scan",
            config_file="apps/config.toml",
            matrix_name="basic",
            results=[_scan_result()],
        )
        assert out.exists()


class TestGitSha:
    def test_returns_short_sha_or_unknown(self) -> None:
        from lora.hwtests.report import git_sha

        sha = git_sha()
        # "unknown" or 7-char short hex
        assert sha == "unknown" or (
            len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha)
        )


class TestInfoErrHelpers:
    def test_info_writes_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        from lora.hwtests.report import info

        info("hello world")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "hello world" in captured.err

    def test_err_writes_error_prefix_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from lora.hwtests.report import err

        err("uh oh")
        captured = capsys.readouterr()
        assert "ERROR: uh oh" in captured.err
