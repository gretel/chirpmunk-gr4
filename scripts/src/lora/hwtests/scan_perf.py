#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""lora_scan sweep performance analyzer.

Phase 5C port of ``lora_perf.py``. Subscribes to
``lora_scan`` CBOR events via UDP and prints per-sweep + rolling
summary lines (grep-friendly):

    SWEEP sweep=1 dur=524ms hot=3 det=1 ovf=0
    DET   sweep=1 n=1 869.618/SF8/BW62k/r=50.2
    SUMMARY sweeps=10 avg_dur=530ms p50_dur=524ms ...
"""

from __future__ import annotations

import argparse
import atexit
import signal
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import cbor2

from lora.core.udp import create_udp_subscriber


@dataclass
class RunningStats:
    """Accumulates cross-sweep statistics."""

    sweep_durations: list[int] = field(default_factory=list)
    hot_counts: list[int] = field(default_factory=list)
    detection_counts: list[int] = field(default_factory=list)
    total_overflows: int = 0
    det_ratios: list[float] = field(default_factory=list)
    det_slopes: dict[str, int] = field(default_factory=dict)
    det_sfs: dict[str, int] = field(default_factory=dict)

    def record_detections(self, dets: list[dict[str, Any]]) -> None:
        for d in dets:
            r = d.get("ratio", 0.0)
            if r > 0:
                self.det_ratios.append(r)
            k = d.get("chirp_slope", 0)
            if k > 0:
                k_str = f"{k / 1000:.0f}k" if k >= 1000 else f"{k:.0f}"
                self.det_slopes[k_str] = self.det_slopes.get(k_str, 0) + 1
            sf = d.get("sf", 0)
            if sf > 0:
                sf_str = f"SF{sf}"
                self.det_sfs[sf_str] = self.det_sfs.get(sf_str, 0) + 1

    def record_sweep(self, dur_ms: int, hot: int, det: int, ovf: int) -> None:
        self.sweep_durations.append(dur_ms)
        self.hot_counts.append(hot)
        self.detection_counts.append(det)
        self.total_overflows = ovf


def _avg(vals: Sequence[int] | Sequence[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _p50(vals: Sequence[int] | Sequence[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def emit_sweep(msg: dict[str, Any]) -> str:
    dur = msg.get("duration_ms", 0)
    hot = msg.get("hot_count", 0)
    det = msg.get("detections", 0)
    ovf = msg.get("overflows", 0)
    sweep_num = msg.get("sweep", 0)
    l1 = msg.get("l1_snapshots", 0)
    return f"SWEEP sweep={sweep_num} dur={dur}ms l1={l1} hot={hot} det={det} ovf={ovf}"


def emit_summary(stats: RunningStats) -> str:
    n = len(stats.sweep_durations)
    if n == 0:
        return "SUMMARY sweeps=0"
    avg_dur = int(_avg(stats.sweep_durations))
    min_dur = min(stats.sweep_durations)
    max_dur = max(stats.sweep_durations)
    p50_dur = int(_p50(stats.sweep_durations))
    avg_hot = _avg(stats.hot_counts)
    total_det = sum(stats.detection_counts)
    avg_ratio = _avg(stats.det_ratios) if stats.det_ratios else 0.0
    min_ratio = min(stats.det_ratios) if stats.det_ratios else 0.0
    max_ratio = max(stats.det_ratios) if stats.det_ratios else 0.0
    slope_str = ",".join(f"{k}:{c}" for k, c in sorted(stats.det_slopes.items()))
    sf_str = ",".join(f"{k}:{c}" for k, c in sorted(stats.det_sfs.items()))
    return (
        f"SUMMARY sweeps={n} "
        f"avg_dur={avg_dur}ms min_dur={min_dur}ms max_dur={max_dur}ms "
        f"p50_dur={p50_dur}ms avg_hot={avg_hot:.1f} "
        f"total_det={total_det} total_ovf={stats.total_overflows}"
        f" avg_ratio={avg_ratio:.1f} min_ratio={min_ratio:.1f} "
        f"max_ratio={max_ratio:.1f}"
        f" sfs={sf_str or '-'} slopes={slope_str or '-'}"
    )


def _emit_l1_diag(msg: dict[str, Any], watch_freq: float = 0.0) -> None:
    channels_raw = msg.get("channels", b"")
    n_ch = msg.get("n_channels", 0)
    freq_min = msg.get("freq_min", 0)
    freq_step = msg.get("freq_step", 62500)
    hot = msg.get("hot", [])
    sweep = msg.get("sweep", 0)
    if not channels_raw or n_ch == 0:
        return
    energies = list(struct.unpack(f"<{n_ch}f", channels_raw[: n_ch * 4]))
    sorted_e = sorted(energies)
    median = sorted_e[len(sorted_e) // 2]
    threshold = median * 6.0
    indexed = [(e, i) for i, e in enumerate(energies)]
    indexed.sort(reverse=True)
    top5 = indexed[:5]
    hot_parts = []
    for h in hot:
        if h < n_ch:
            freq_mhz = (freq_min + (h + 0.5) * freq_step) / 1e6
            ratio_to_median = energies[h] / median if median > 0 else 0
            hot_parts.append(f"ch{h}={freq_mhz:.3f}MHz({ratio_to_median:.1f}x)")
    top_parts = []
    for e, i in top5:
        freq_mhz = (freq_min + (i + 0.5) * freq_step) / 1e6
        ratio_to_median = e / median if median > 0 else 0
        marker = "*" if i in hot else ""
        top_parts.append(f"ch{i}{marker}={freq_mhz:.3f}MHz({ratio_to_median:.1f}x)")
    watch_str = ""
    if watch_freq > 0 and freq_step > 0:
        watch_ch = int((watch_freq - freq_min) / freq_step)
        if 0 <= watch_ch < n_ch:
            w_ratio = energies[watch_ch] / median if median > 0 else 0
            w_freq = (freq_min + (watch_ch + 0.5) * freq_step) / 1e6
            watch_str = f" WATCH ch{watch_ch}={w_freq:.3f}MHz({w_ratio:.1f}x)"
    print(
        f"  L1 sweep={sweep} median={median:.2e} thresh={threshold:.2e} "
        f"hot=[{','.join(hot_parts)}] "
        f"top5=[{', '.join(top_parts)}]{watch_str}",
        flush=True,
    )


def main(
    argv: list[str] | None = None,
) -> int:  # pragma: no cover  # UDP loop, lora_scan-bound
    parser = argparse.ArgumentParser(
        prog="lora hwtest scan-perf",
        description="lora_scan sweep performance analyzer.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="lora_scan UDP host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5557,
        help="lora_scan UDP port (default: 5557)",
    )
    parser.add_argument(
        "--diag",
        action="store_true",
        help="print L1 energy diagnostics per sweep",
    )
    parser.add_argument(
        "--watch-freq",
        type=float,
        default=0.0,
        help="frequency (Hz) to monitor in L1 diag (e.g. 869618000)",
    )
    args = parser.parse_args(argv)

    sock, _sub_msg, _addr = create_udp_subscriber(args.host, args.port)
    stats = RunningStats()
    summary_interval = 10

    def print_final_summary() -> None:
        if stats.sweep_durations:
            print(emit_summary(stats), flush=True)

    atexit.register(print_final_summary)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    while True:
        data, _ = sock.recvfrom(65536)
        try:
            msg = cbor2.loads(data)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type", "")
        if msg_type == "scan_spectrum":
            dets = [d for d in msg.get("detections", []) if isinstance(d, dict)]
            if dets:
                stats.record_detections(dets)
                sweep_num = msg.get("sweep", 0)
                det_parts: list[str] = []
                for d in dets:
                    freq_mhz = d.get("freq", 0) / 1e6
                    ratio = d.get("ratio", 0.0)
                    sf = d.get("sf", 0)
                    sf_str = f"SF{sf}" if sf else "?"
                    probe_bw = d.get("probe_bw", 0)
                    probe_str = f"BW{probe_bw / 1000:.0f}k" if probe_bw else ""
                    sync = d.get("sync_word")
                    if sync is not None:
                        cfo_f = d.get("cfo_frac", 0.0)
                        cfo_i = d.get("cfo_int", 0)
                        snr = d.get("snr_db", -999)
                        det_parts.append(
                            f"{freq_mhz:.3f}MHz {sf_str} {probe_str} "
                            f"sync=0x{sync:02X} CFO={cfo_f:+.1f}+{cfo_i} "
                            f"SNR={snr:.1f} r={ratio:.1f}"
                        )
                    else:
                        k = d.get("chirp_slope", 0)
                        k_str = f"{k / 1000:.0f}k" if k >= 1000 else f"{k:.0f}"
                        det_parts.append(
                            f"{freq_mhz:.3f}MHz {sf_str} "
                            f"r={ratio:.1f}/k={k_str}/@{probe_str}"
                        )
                print(
                    f"DET sweep={sweep_num} n={len(dets)} {' '.join(det_parts)}",
                    flush=True,
                )
            if args.diag:
                _emit_l1_diag(msg, watch_freq=args.watch_freq)
        elif msg_type == "scan_sweep_end":
            print(emit_sweep(msg), flush=True)
            stats.record_sweep(
                dur_ms=msg.get("duration_ms", 0),
                hot=msg.get("hot_count", 0),
                det=msg.get("detections", 0),
                ovf=msg.get("overflows", 0),
            )
            if len(stats.sweep_durations) % summary_interval == 0:
                print(emit_summary(stats), flush=True)
        elif msg_type == "scan_overflow":
            stats.total_overflows = msg.get("count", stats.total_overflows)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
