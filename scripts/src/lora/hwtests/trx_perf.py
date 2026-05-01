#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""lora_trx real-time frame monitor.

Phase 5C port of ``trx_perf.py``. Subscribes to
``lora_trx`` CBOR events via UDP and prints one line per decoded frame
plus periodic summaries. Useful for interactive debugging.

Output lines (grep-friendly):

    FRAME seq=1 SF8 BW62.5k CRC=OK SNR=12.3 freq=869.618 cfo_int=-2
    SUMMARY frames=10 crc_ok=8 crc_fail=2 avg_snr=10.5
"""

from __future__ import annotations

import argparse
import atexit
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import cbor2

from lora.core.udp import create_udp_subscriber


@dataclass
class FrameStats:
    """Accumulates cross-frame statistics."""

    frame_count: int = 0
    dedup_count: int = 0
    crc_ok: int = 0
    crc_fail: int = 0
    snr_values: list[float] = field(default_factory=list)
    sf_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class WidebandStats:
    """Accumulates wideband sweep statistics."""

    sweep_count: int = 0
    tainted_count: int = 0
    sweep_durations: list[int] = field(default_factory=list)
    hot_counts: list[int] = field(default_factory=list)


def format_bw(bw: float) -> str:
    """Format BW as human-readable (62500 → '62.5k')."""
    if bw >= 1000:
        val = bw / 1000
        if val == int(val):
            return f"{int(val)}k"
        return f"{val:g}k"
    return str(int(bw))


def emit_frame(msg: dict[str, Any]) -> str:
    phy = msg.get("phy", {})
    seq = msg.get("seq", 0)
    sf = phy.get("sf", "?")
    bw = phy.get("bw", 0)
    crc_ok = msg.get("crc_valid", False)
    snr = phy.get("snr_db", 0.0)
    channel_freq = phy.get("channel_freq", 0.0)
    cfo_int = phy.get("cfo_int", None)
    cfo_frac = phy.get("cfo_frac", None)
    sfo_hat = phy.get("sfo_hat", None)
    decode_bw = phy.get("decode_bw", 0)

    bw_str = f"BW{format_bw(bw)}" if bw else ""
    crc_str = "OK" if crc_ok else "FAIL"
    parts = [f"FRAME seq={seq} SF{sf} {bw_str} CRC={crc_str} SNR={snr:.1f}"]

    if channel_freq > 0:
        parts.append(f"freq={channel_freq / 1e6:.3f}")
    if decode_bw > 0:
        parts.append(f"dec_bw={format_bw(decode_bw)}")
    if cfo_int is not None:
        parts.append(f"cfo_int={int(cfo_int)}")
    if cfo_frac is not None:
        parts.append(f"cfo_frac={cfo_frac:.3f}")
    if sfo_hat is not None:
        parts.append(f"sfo={sfo_hat:.3f}")
    payload = msg.get("payload", b"")
    if payload:
        parts.append(f"len={len(payload)}")
    return " ".join(parts)


def emit_summary(stats: FrameStats) -> str:
    n = stats.frame_count
    if n == 0:
        return "SUMMARY frames=0"
    avg_snr = sum(stats.snr_values) / len(stats.snr_values) if stats.snr_values else 0.0
    sf_str = ",".join(f"SF{sf}:{c}" for sf, c in sorted(stats.sf_counts.items()))
    return (
        f"SUMMARY frames={n} crc_ok={stats.crc_ok} crc_fail={stats.crc_fail} "
        f"avg_snr={avg_snr:.1f} sfs={sf_str or '-'}"
    )


def main(
    argv: list[str] | None = None,
) -> int:  # pragma: no cover  # UDP loop, lora_trx-bound
    parser = argparse.ArgumentParser(
        prog="lora hwtest trx-perf",
        description="lora_trx real-time frame monitor.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="lora_trx UDP host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5556,
        help="lora_trx UDP port (default: 5556)",
    )
    args = parser.parse_args(argv)

    sock, _sub_msg, _addr = create_udp_subscriber(args.host, args.port)
    stats = FrameStats()
    wb_stats = WidebandStats()
    summary_interval = 10

    def print_final_summary() -> None:
        parts: list[str] = []
        if stats.frame_count > 0:
            parts.append(emit_summary(stats))
        if wb_stats.sweep_count > 0:
            avg_dur = sum(wb_stats.sweep_durations) / len(wb_stats.sweep_durations)
            avg_hot = sum(wb_stats.hot_counts) / len(wb_stats.hot_counts)
            parts.append(
                f"WB_SUMMARY sweeps={wb_stats.sweep_count} "
                f"tainted={wb_stats.tainted_count} "
                f"avg_dur={avg_dur:.0f}ms avg_hot={avg_hot:.1f}"
            )
        for p in parts:
            print(p, flush=True)

    # Cross-BW frame dedup: multi-BW decode can yield duplicate decodes
    # of the same packet via different MultiSfDecoders. Suppress dupes
    # by payload_hash within a 1-second window.
    recent_hashes: dict[int, float] = {}

    def is_duplicate(phash: int) -> bool:
        now = time.monotonic()
        stale = [k for k, t in recent_hashes.items() if now - t > 1.0]
        for k in stale:
            del recent_hashes[k]
        if phash in recent_hashes:
            return True
        recent_hashes[phash] = now
        return False

    atexit.register(print_final_summary)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    while True:
        try:
            data, _ = sock.recvfrom(65536)
        except TimeoutError:
            print("No data received, exiting.", file=sys.stderr)
            break
        try:
            msg = cbor2.loads(data)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type", "")

        if msg_type == "lora_frame":
            phash = msg.get("payload_hash", 0)
            if phash and is_duplicate(phash):
                stats.dedup_count += 1
                continue
            phy = msg.get("phy", {})
            crc_ok = msg.get("crc_valid", False)
            snr = phy.get("snr_db", 0.0)
            sf = phy.get("sf", 0)
            stats.frame_count += 1
            if crc_ok:
                stats.crc_ok += 1
            else:
                stats.crc_fail += 1
            stats.snr_values.append(snr)
            if sf > 0:
                stats.sf_counts[sf] = stats.sf_counts.get(sf, 0) + 1
            print(emit_frame(msg), flush=True)
            if stats.frame_count % summary_interval == 0:
                print(emit_summary(stats), flush=True)

        elif msg_type == "wideband_sweep":
            sweep = msg.get("sweep", 0)
            dur = msg.get("duration_ms", 0)
            n_hot = msg.get("n_hot", 0)
            n_active = msg.get("n_active", 0)
            max_slots = msg.get("max_slots", 0)
            tainted = msg.get("tainted", False)
            wb_stats.sweep_count += 1
            wb_stats.sweep_durations.append(dur)
            wb_stats.hot_counts.append(n_hot)
            if tainted:
                wb_stats.tainted_count += 1
            ovf = msg.get("overflows", 0)
            tag = " TAINTED" if tainted else ""
            print(
                f"WB_SWEEP sweep={sweep} dur={dur}ms "
                f"hot={n_hot} active={n_active}/{max_slots} ovf={ovf}{tag}",
                flush=True,
            )
            if wb_stats.sweep_count % summary_interval == 0:
                avg_dur = sum(wb_stats.sweep_durations) / len(wb_stats.sweep_durations)
                avg_hot = sum(wb_stats.hot_counts) / len(wb_stats.hot_counts)
                print(
                    f"WB_SUMMARY sweeps={wb_stats.sweep_count} "
                    f"tainted={wb_stats.tainted_count} "
                    f"avg_dur={avg_dur:.0f}ms avg_hot={avg_hot:.1f}",
                    flush=True,
                )

        elif msg_type == "wideband_slot":
            action = msg.get("action", "?")
            slot = msg.get("slot", 0)
            freq = msg.get("freq", 0)
            bw = msg.get("bw", 0)
            freq_mhz = freq / 1e6 if freq else 0
            bw_str = format_bw(bw) if bw else "?"
            print(
                f"WB_SLOT {action} slot={slot} freq={freq_mhz:.3f}MHz BW{bw_str}",
                flush=True,
            )

        elif msg_type == "multisf_detect":
            sf = msg.get("sf", 0)
            bin_val = msg.get("bin", 0)
            print(f"DECODE_DETECT SF{sf} bin={bin_val}", flush=True)

        elif msg_type == "multisf_sync":
            sf = msg.get("sf", 0)
            cfo = msg.get("cfo_int", 0)
            cfo_frac = msg.get("cfo_frac", 0.0)
            snr = msg.get("snr_db", 0.0)
            print(
                f"DECODE_SYNC SF{sf} cfo_int={cfo} cfo_frac={cfo_frac:.3f} "
                f"SNR={snr:.1f}",
                flush=True,
            )

        elif msg_type == "multisf_frame":
            sf = msg.get("sf", 0)
            crc = "OK" if msg.get("crc_ok", False) else "FAIL"
            snr = msg.get("snr_db", 0.0)
            length = msg.get("len", 0)
            cr = msg.get("cr", 0)
            print(
                f"DECODE_FRAME SF{sf} CRC={crc} SNR={snr:.1f} CR={cr} len={length}",
                flush=True,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
