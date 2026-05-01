#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""lora.viz.spectrum — real-time LoRa scan-spectrum bar graph.

Usage:
    python3 lora_spectrum.py [--host HOST] [--port PORT] [--ema ALPHA]

Keyboard Controls:
    Space  - Pause/resume
    +/-    - Adjust EMA alpha (sensitivity)
    q      - Quit
    0-9    - Set EMA to 0.0-0.9
"""

from __future__ import annotations

import argparse
import atexit
import io
import math
import os
import select
import struct
import sys
import termios
import time
import tty
from typing import Any

import cbor2
from setproctitle import setproctitle

from lora.core.udp import create_udp_subscriber

# ─── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_EMA_ALPHA = 0.3
DB_HEADROOM = 1.0
DB_FLOOR_PAD = 3.0
ANCHOR_TTL = 3.0
PEAK_HOLD_S = 5.0
HEADER_LINES = 2 + 1

# ANSI colors
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RST = "\033[0m"
C_CYAN = "\033[38;5;87m"
C_GREEN = "\033[38;5;46m"
C_YELLOW = "\033[38;5;226m"
C_ORANGE = "\033[38;5;214m"
C_RED = "\033[38;5;196m"
C_BLUE = "\033[38;5;39m"
C_WHITE = "\033[38;5;255m"
C_GRAY = "\033[38;5;242m"
C_MAGENTA = "\033[38;5;213m"

# Energy gradient
_GRAD = [
    "\033[38;5;17m",
    "\033[38;5;24m",
    "\033[38;5;30m",
    "\033[38;5;35m",
    "\033[38;5;40m",
    "\033[38;5;154m",
    "\033[38;5;226m",
    "\033[38;5;214m",
    "\033[38;5;202m",
    "\033[38;5;196m",
]
_N_GRAD = len(_GRAD)
_BAR = "\u2588"

# ─── Terminal I/O ────────────────────────────────────────────────────────────

_tty: Any = None
_old_termios: Any | None = None


def _get_tty() -> Any:
    """Get or create TTY file handle.

    Returns either an ``io.FileIO`` wrapping ``/dev/tty`` (preferred,
    keeps stderr clean) or ``sys.stderr.buffer`` as fallback. Both
    expose ``write(bytes)`` and ``flush()`` so callers don't care
    which one they get.
    """
    global _tty
    if _tty is None:
        try:
            fd = os.open("/dev/tty", os.O_WRONLY)
            _tty = io.FileIO(fd, mode="wb")
        except OSError:
            _tty = sys.stderr.buffer
    assert _tty is not None
    return _tty


def _write(s: str) -> None:
    _get_tty().write(s.encode())
    _get_tty().flush()


def _cleanup() -> None:
    global _old_termios
    try:
        _write("\033[?25h\033[0m\n")
        if _old_termios:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_termios)
    except OSError:
        pass


atexit.register(_cleanup)


def enable_raw_input() -> None:
    """Enable raw terminal mode for single-key input."""
    global _old_termios
    try:
        _old_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    except Exception:
        pass


def check_key() -> str | None:
    """Check for pending keypress (non-blocking)."""
    try:
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
    except Exception:
        pass
    return None


# ─── State ───────────────────────────────────────────────────────────────────


class State:
    """Spectrum and sweep state."""

    def __init__(self, ema_alpha: float = DEFAULT_EMA_ALPHA) -> None:
        self.energy: list[float] | None = None
        self.raw_energy: list[float] | None = None
        self.freq_min: int = 0
        self.freq_step: int = 62500
        self.n_channels: int = 0
        self.hot: set[int] = set()
        self.ema_alpha: float = ema_alpha
        self.paused: bool = False

        self.sweeps: int = 0
        self.total_det: int = 0
        self.overflows: int = 0
        self.last_sweep_ms: int = 0
        self.sweep_rate: float = 0.0
        self._prev_spectrum_time: float = 0.0
        self._prev_sweep_count: int = 0

        self.active_dets: list[tuple[float, dict[str, Any]]] = []
        self.peak_holds: list[tuple[int, float, float, str, str]] = []

    def on_spectrum(self, msg: dict[str, Any]) -> None:
        if self.paused:
            return

        n = msg["n_channels"]
        raw = list(struct.unpack(f"<{n}f", msg["channels"]))
        self.raw_energy = raw

        if self.energy is None or len(self.energy) != n:
            self.energy = raw
        else:
            a = self.ema_alpha
            self.energy = [a * r + (1 - a) * e for r, e in zip(raw, self.energy)]

        self.freq_min = msg["freq_min"]
        self.freq_step = msg["freq_step"]
        self.n_channels = n
        self.hot = set(msg.get("hot", []))

        mono = time.monotonic()
        sweep = msg.get("sweep", self.sweeps)
        dets = msg.get("detections", [])

        for det in dets:
            self.active_dets.append((mono, det))
            freq = det.get("freq", 0)
            if self.freq_step > 0 and freq > 0:
                ch = int((freq - self.freq_min) / self.freq_step + 0.5)
                ch = max(0, min(ch, n - 1))
                if self.raw_energy:
                    db_val = 10.0 * math.log10(max(self.raw_energy[ch], 1e-12))
                    ratio = det.get("ratio", 0.0)
                    sw = det.get("sync_word")
                    parts = [f"r{ratio:.0f}"]
                    if sw is not None:
                        parts.append(f"0x{sw:02X}")
                    lbl = " ".join(parts)
                    color = (
                        C_GREEN
                        if ratio >= 50
                        else C_YELLOW
                        if ratio >= 20
                        else C_ORANGE
                    )
                    self.peak_holds.append((ch, db_val, mono, lbl, color))

        # Prune old
        self.active_dets = [
            (t, d) for t, d in self.active_dets if mono - t < ANCHOR_TTL
        ]
        self.peak_holds = [
            (ch, db, t, lbl, lc)
            for ch, db, t, lbl, lc in self.peak_holds
            if mono - t < PEAK_HOLD_S
        ]

        self.sweeps = sweep
        if self._prev_spectrum_time > 0:
            dt = mono - self._prev_spectrum_time
            ds = self.sweeps - self._prev_sweep_count
            if dt > 0.1 and ds > 0:
                self.sweep_rate = ds / dt
        self._prev_spectrum_time = mono
        self._prev_sweep_count = self.sweeps

    def on_sweep_end(self, msg: dict[str, Any]) -> None:
        self.last_sweep_ms = msg.get("duration_ms", 0)
        self.sweeps = msg.get("sweep", self.sweeps)
        self.total_det += msg.get("detections", 0)
        ovf = msg.get("overflows", 0)
        if ovf > 0:
            self.overflows = ovf

    def on_status(self, msg: dict[str, Any]) -> None:
        self.total_det = msg.get("detections", self.total_det)
        self.overflows = msg.get("overflows", self.overflows)

    def reset(self) -> None:
        self.energy = None
        self.raw_energy = None
        self.active_dets = []
        self.peak_holds = []
        self.total_det = 0


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _db(e: float) -> float:
    return 10.0 * math.log10(max(e, 1e-12))


def _resample(energy: list[float], cols: int) -> list[float]:
    n = len(energy)
    if n == 0:
        return [0.0] * cols
    if n <= cols:
        out: list[float] = []
        for i, e in enumerate(energy):
            lo = i * cols // n
            hi = (i + 1) * cols // n
            out.extend([e] * (hi - lo))
        return out[:cols]
    out = []
    for c in range(cols):
        lo = c * n // cols
        hi = (c + 1) * n // cols
        out.append(sum(energy[lo:hi]) / max(1, hi - lo))
    return out


def _grad_color(frac: float) -> str:
    idx = min(int(frac * _N_GRAD), _N_GRAD - 1)
    return _GRAD[idx]


def _ratio_color(ratio: float) -> str:
    if ratio >= 50:
        return C_GREEN
    if ratio >= 20:
        return C_YELLOW
    return C_ORANGE


def _sweep_rate_color(rate: float) -> str:
    if rate >= 0.8:
        return C_GREEN
    if rate >= 0.4:
        return C_YELLOW
    return C_RED


# ─── Rendering ─────────────────────────────────────────────────────────────────


def _term_size() -> tuple[int, int]:
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        size = os.get_terminal_size(fd)
        os.close(fd)
        return size.columns, size.lines
    except OSError:
        return 80, 24


def _render_header(s: State, term_w: int) -> str:
    parts: list[str] = []
    parts.append(f"{C_WHITE}lora_spectrum{C_RST}")

    sw_str = f"{s.sweeps:>5}"
    if s.sweep_rate > 0:
        rc = _sweep_rate_color(s.sweep_rate)
        rate_str = f" {rc}{s.sweep_rate:.1f}{C_RST}"
    else:
        rate_str = ""
    parts.append(
        f"  {C_BLUE}sw{C_RST} {C_WHITE}{sw_str}{C_RST}  {C_BLUE}sw/s{C_RST}{rate_str}"
    )

    if s.last_sweep_ms > 0:
        dur_c = (
            C_GREEN
            if s.last_sweep_ms < 1500
            else C_YELLOW
            if s.last_sweep_ms < 3000
            else C_RED
        )
        parts.append(f"  {dur_c}{s.last_sweep_ms:>5}ms{C_RST}")

    det_c = C_MAGENTA if s.total_det > 0 else C_GRAY
    parts.append(f"  {C_BLUE}det{C_RST} {det_c}{s.total_det:>4}{C_RST}")

    if s.overflows > 0:
        parts.append(f"  {C_RED}OVF {s.overflows}{C_RST}")

    if s.n_channels > 0:
        f_lo = s.freq_min / 1e6
        f_hi = (s.freq_min + s.freq_step * s.n_channels) / 1e6
        parts.append(f"  {C_CYAN}{f_lo:.3f}\u2013{f_hi:.3f} MHz{C_RST}")

    import time

    parts.append(f"  {C_GRAY}{time.strftime('%H:%M:%S')}{C_RST}")
    parts.append(f"  {C_GRAY}a={s.ema_alpha:.1f}{C_RST}")

    return "".join(parts) + "\033[K\n"


def render(s: State) -> None:
    """Full redraw."""
    if s.energy is None or s.raw_energy is None:
        return

    term_w, term_h = _term_size()
    lbl_w = 8
    spec_w = max(10, term_w - lbl_w)
    body_h = max(2, term_h - HEADER_LINES - 1)

    col_e_smooth = _resample(s.energy, spec_w)
    col_e_raw = _resample(s.raw_energy, spec_w)
    n_cols = len(col_e_smooth)

    db_raw = [_db(e) for e in col_e_raw]
    nonzero = sorted(d for d, e in zip(db_raw, col_e_raw) if e > 1e-15)
    db_min = nonzero[len(nonzero) // 4] - DB_FLOOR_PAD if nonzero else -100.0
    db_max = max(db_raw) + DB_HEADROOM
    db_range = max(db_max - db_min, 10.0)

    bar_h = [
        min(body_h, max(0, int((db_raw[c] - db_min) / db_range * body_h + 0.5)))
        for c in range(n_cols)
    ]

    db_smooth = [_db(e) for e in col_e_smooth]
    db_min_s, db_max_s = min(db_smooth), max(db_smooth)
    db_range_s = max(db_max_s - db_min_s, 10.0)
    col_color = [
        _grad_color((db_smooth[c] - db_min_s) / db_range_s) for c in range(n_cols)
    ]

    det_cols: set[int] = set()
    for _t, det in s.active_dets:
        freq = det["freq"]
        if s.freq_step > 0:
            ch_idx = (freq - s.freq_min) / s.freq_step
            col = max(0, min(spec_w - 1, int(ch_idx * spec_w / s.n_channels)))
            det_cols.add(col)

    _peak_raw: dict[int, tuple[int, str, str]] = {}
    mono_now = time.monotonic()
    for ch_idx, peak_db, t, lbl, lbl_color in s.peak_holds:
        age = mono_now - t
        if age >= PEAK_HOLD_S:
            continue
        col = int(ch_idx * spec_w / s.n_channels) if s.n_channels > 0 else 0
        col = max(0, min(col, spec_w - 1))
        peak_row = body_h - int((peak_db - db_min) / db_range * body_h + 0.5)
        peak_row = max(0, min(peak_row, body_h - 1))
        if col not in _peak_raw or peak_row < _peak_raw[col][0]:
            _peak_raw[col] = (peak_row, lbl, lbl_color)

    peak_hold_rows: dict[int, tuple[int, str, str]] = {}
    if _peak_raw:
        sorted_peaks = sorted(_peak_raw.items())
        clusters: list[list[tuple[int, tuple[int, str, str]]]] = []
        for item in sorted_peaks:
            if clusters and item[0] - clusters[-1][-1][0] <= 2:
                clusters[-1].append(item)
            else:
                clusters.append([item])
        for cluster in clusters:
            median_col = cluster[len(cluster) // 2][0]
            best_row = min(c[1][0] for c in cluster)
            best_entry = min(cluster, key=lambda c: c[1][0])
            peak_hold_rows[median_col] = (best_row, best_entry[1][1], best_entry[1][2])

    out: list[str] = ["\033[H"]
    out.append(_render_header(s, term_w))

    f_lo = s.freq_min / 1e6
    f_hi = (s.freq_min + s.n_channels * s.freq_step) / 1e6
    f_mid = (f_lo + f_hi) / 2
    left, mid, right = f"{f_lo:.1f}", f"{f_mid:.1f}", f"{f_hi:.1f} MHz"
    pad1 = max(1, spec_w // 2 - len(left) - len(mid) // 2)
    pad2 = max(1, spec_w - len(left) - pad1 - len(mid) - len(right))
    out.append(
        f"{' ' * lbl_w}{C_CYAN}{left}{' ' * pad1}{mid}{' ' * pad2}{right}{C_RST}\033[K\n"
    )

    for row in range(body_h):
        if row == 0:
            label = f"{db_max:6.0f}dB"
        elif row == body_h - 1:
            label = f"{db_min:6.0f}dB"
        elif body_h > 6 and row % max(1, body_h // 4) == 0:
            label = f"{db_max - (row / body_h) * db_range:6.0f}dB"
        else:
            label = ""
        out.append(f"{C_GRAY}{label:>{lbl_w}}{C_RST}")

        row_overlays: dict[int, tuple[str, str]] = {}
        for mc, (marker_row, freq_label, color) in peak_hold_rows.items():
            if marker_row == row:
                row_overlays[mc] = ("\u25bc", C_MAGENTA)
            if freq_label and marker_row - 1 == row:
                for i, ch in enumerate(freq_label):
                    c = mc + i
                    if 0 <= c < n_cols and c not in row_overlays:
                        row_overlays[c] = (ch, color)

        threshold = body_h - row
        prev_color, run_len = "", 0
        for col in range(n_cols):
            has_bar = bar_h[col] >= threshold and col_e_raw[col] > 1e-15
            overlay = row_overlays.get(col)

            if overlay and not has_bar:
                if run_len:
                    out.append(f"{prev_color}{_BAR * run_len}{C_RST}")
                    prev_color, run_len = "", 0
                ch, oc = overlay
                out.append(f"{oc}{ch}{C_RST}")
            elif has_bar:
                cc = C_MAGENTA if col in det_cols else col_color[col]
                if cc == prev_color:
                    run_len += 1
                else:
                    if run_len:
                        out.append(f"{prev_color}{_BAR * run_len}")
                    prev_color, run_len = cc, 1
            else:
                if run_len:
                    out.append(f"{prev_color}{_BAR * run_len}{C_RST}")
                    prev_color, run_len = "", 0
                out.append(" ")
        if run_len:
            out.append(f"{prev_color}{_BAR * run_len}{C_RST}")
        out.append("\033[K\n")

    out.append("\033[J")
    _write("".join(out))


# ─── Main ──────────────────────────────────────────────────────────────────────


def handle_key(key: str, state: State) -> bool:
    """Handle keyboard input. Returns True to quit."""
    if key == "q":
        return True
    elif key == " ":
        state.paused = not state.paused
    elif key == "+" or key == "=":
        state.ema_alpha = min(0.9, state.ema_alpha + 0.1)
    elif key == "-":
        state.ema_alpha = max(0.0, state.ema_alpha - 0.1)
    elif key.isdigit():
        state.ema_alpha = int(key) / 10.0
    return False


def main(argv: list[str] | None = None) -> int:
    setproctitle("lora-spectrum")

    parser = argparse.ArgumentParser(
        prog="lora spectrum",
        description="LoRa scan-spectrum bar graph via UDP/CBOR (lora_scan source)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="UDP host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5557,
        help="UDP port (default: 5557 — lora_scan, not lora-core)",
    )
    parser.add_argument(
        "--ema",
        type=float,
        default=DEFAULT_EMA_ALPHA,
        help=f"EMA alpha 0.0-1.0 (default: {DEFAULT_EMA_ALPHA})",
    )
    args = parser.parse_args(argv)

    _write("\033[?25l")  # Hide cursor
    enable_raw_input()

    sock, _sub_msg, _addr = create_udp_subscriber(args.host, args.port)
    state = State(ema_alpha=args.ema)

    dispatch = {
        "scan_spectrum": state.on_spectrum,
        "scan_sweep_end": state.on_sweep_end,
        "scan_status": state.on_status,
    }

    header_types = {
        "scan_status",
        "scan_sweep_start",
        "scan_sweep_end",
        "scan_l2_probe",
        "scan_overflow",
    }
    first = True

    try:
        while True:
            # Check keyboard
            key = check_key()
            if key:
                if handle_key(key, state):
                    break
                # Force render on keypress to show feedback
                if not first:
                    render(state)
                    continue

            # Receive with timeout for responsive keyboard
            sock.settimeout(0.05)
            try:
                data, _ = sock.recvfrom(65536)
            except TimeoutError:
                continue

            try:
                msg = cbor2.loads(data)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type", "")
            handler = dispatch.get(msg_type)
            if handler:
                handler(msg)

            if msg_type == "scan_spectrum" and not state.paused:
                if first:
                    _write("\033[2J")
                    first = False
                render(state)
            elif msg_type in header_types and not first:
                # Update header only
                term_w, _ = _term_size()
                _write(f"\033[H{_render_header(state, term_w)}")

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
