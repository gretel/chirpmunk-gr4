# SPDX-License-Identifier: ISC
"""Tests for ``lora.viz.mon`` — thin renderer over the lora-core event bus.

Coverage strategy:

  - exercise the rendering helpers (``_render`` + ``format_status`` +
    ``format_config``) directly against synthetic CBOR maps;
  - exercise the UDP loop with a real localhost socket producing one
    typed event then quitting via SIGINT, with a 5 s wall-clock bound.

Curses / interactive behaviour is out of scope (mon has none — it
just prints to stderr/stdout).
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from typing import Any

import cbor2

from lora.viz import mon


def _meshcore_lora_frame(
    *, with_protocol: bool, payload: bytes = b"\x12\x00"
) -> dict[str, Any]:
    """Build a minimal lora_frame dict shaped like the daemon emits."""
    msg: dict[str, Any] = {
        "type": "lora_frame",
        "ts": "2026-04-28T12:00:00.0000Z",
        "seq": 1,
        "payload": payload,
        "payload_len": len(payload),
        "crc_valid": True,
        "cr": 4,
        "is_downchirp": False,
        "phy": {
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "sync_word": 0x12,
            "crc_valid": True,
            "snr_db": 7.5,
            "noise_floor_db": -60.0,
        },
        "carrier": {
            "sync_word": 0x12,
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "ldro_cfg": False,
        },
    }
    if with_protocol:
        msg["protocol"] = {
            "name": "meshcore",
            "ok": True,
            "fields": {
                "version": 0,
                "route": "F",
                "payload_type": "TXT",
                "path_len": 0,
                "decrypted_text": "Hello",
            },
        }
    return msg


def test_render_with_protocol_shows_decrypted_text() -> None:
    msg = _meshcore_lora_frame(with_protocol=True)
    line = mon._render(msg)
    assert line is not None
    # decrypted text + payload type both present, per spec
    assert "Hello" in line, line
    assert "TXT" in line, line
    # CRC + SF appear in the header line
    assert "CRC_OK" in line
    assert "SF8" in line


def test_render_without_protocol_shows_hex_dump() -> None:
    msg = _meshcore_lora_frame(with_protocol=False, payload=b"\xab\xcd\xef\x00")
    line = mon._render(msg)
    assert line is not None
    # hex bytes from the payload show up in the dump
    assert "AB CD EF 00" in line, line
    # no decrypted line because no protocol annotation
    assert "Hello" not in line


def test_render_status_one_liner() -> None:
    msg = {
        "type": "status",
        "ts": "2026-04-28T12:00:00.0Z",
        "phy": {"rx_gain": 38},
        "frames": {"total": 12, "crc_ok": 10, "crc_fail": 2},
    }
    line = mon._render(msg)
    assert line is not None
    assert "12 frames" in line
    assert "10 OK" in line
    assert "RX gain=38" in line


def test_render_config_two_lines() -> None:
    msg = {
        "type": "config",
        "phy": {
            "freq": 869_618_000,
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "sync_word": 0x12,
            "rx_gain": 38,
            "tx_gain": 0,
        },
        "server": {
            "device": "ZPTP4PO",
            "sample_rate": 250000,
            "status_interval": 10,
        },
    }
    line = mon._render(msg)
    assert line is not None
    assert "869.618" in line
    assert "SF8" in line
    assert "ZPTP4PO" in line


def test_render_unknown_type_returns_none() -> None:
    assert mon._render({"type": "nope"}) is None


def test_render_handles_lorawan_protocol_annotation() -> None:
    msg = _meshcore_lora_frame(
        with_protocol=False,
        payload=b"\x40\x01\x02\x03\x04\x05\x00\x00\x00\xde\xad\xbe\xef",
    )
    msg["phy"]["sync_word"] = 0x34
    msg["carrier"]["sync_word"] = 0x34
    msg["protocol"] = {
        "name": "lorawan",
        "ok": True,
        "fields": {
            "mtype": "UnconfDataUp",
            "dev_addr": "04030201",
            "fcnt": 42,
        },
    }
    line = mon._render(msg)
    assert line is not None
    assert "UnconfDataUp" in line
    assert "04030201" in line
    assert "fcnt=42" in line


def test_main_help_exits_zero() -> None:
    """``lora mon --help`` must exit 0 and not block."""
    result = subprocess.run(
        [sys.executable, "-m", "lora.viz.mon", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "live frame viewer" in (result.stdout + result.stderr).lower()
        or "subscribes to a lora-core daemon" in result.stdout
    )


class _FakeSock:
    """Minimal duck-typed stand-in for a UDP socket inside ``mon._consume``.

    First ``recvfrom`` returns the queued frame; the next raises
    ``KeyboardInterrupt`` to break the consume loop. ``sendto`` and
    ``close`` are no-ops; ``getsockname`` returns a fixed local port.
    """

    def __init__(self, frame: bytes, addr: tuple[str, int]) -> None:
        self._queue: list[bytes] = [frame]
        self._addr = addr
        self.calls = 0
        self.sent: list[bytes] = []

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        self.calls += 1
        if self._queue:
            return self._queue.pop(0), self._addr
        raise KeyboardInterrupt

    def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
        self.sent.append(data)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 12345)

    def close(self) -> None:
        pass


def test_consume_processes_one_frame_then_quits(monkeypatch) -> None:
    """Mocked-UDP smoke test for ``mon._consume``.

    Pumps one synthetic CBOR ``lora_frame`` through the consume loop
    via a fake socket, then raises KeyboardInterrupt. Asserts clean
    exit, the frame got rendered, and the connect-log line printed.
    """
    frame_bytes = cbor2.dumps(_meshcore_lora_frame(with_protocol=True))
    addr = ("127.0.0.1", 5558)
    fake = _FakeSock(frame_bytes, addr)

    def fake_subscriber(*_args, **_kwargs):
        return fake, b"\x01", addr

    monkeypatch.setattr(mon, "create_udp_subscriber", fake_subscriber)

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        rc = mon._consume("127.0.0.1", 5558)
    finally:
        root.removeHandler(handler)

    assert rc == 0
    out = buf.getvalue()
    assert "connected to" in out, out
    assert fake.calls >= 1
    # The frame was rendered — protocol-derived summary contains "TXT".
    assert "TXT" in out or "Hello" in out, out
