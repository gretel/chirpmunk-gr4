# SPDX-License-Identifier: ISC
"""End-to-end multi-source A/B integration test.

Wires two synthetic upstream UDP servers into a real
:func:`lora.daemon.main.run_daemon` and verifies cross-source dedup +
SNR-winner ranking + DuckDB single-row guarantee.

The synthetic upstreams emit the SAME ``lora_frame`` (matching
``payload_hash`` and ``id``) at different ``phy.snr_db`` values so the
diversity ranking is deterministic: ``radio_2g4`` (snr=12.0) beats
``radio_868`` (snr=5.0). The aggregator's ``window_ms`` is dropped to
40ms so the test runs well under its 5s cap.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any

import cbor2
import duckdb
import pytest

from lora.daemon.config import DaemonConfig, UpstreamConfig
from lora.daemon.main import run_daemon
from lora.identity import IdentityConfig
from lora.storage import StorageConfig

pytestmark = pytest.mark.timeout(10)


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _make_lora_frame(snr_db: float, source: str) -> dict[str, Any]:
    """Build a CBOR-shaped lora_frame dict.

    Same ``payload`` (so payload_hash matches across sources) and same
    ``id`` so the DuckDB row count is unambiguous; the only varying
    field is ``phy.snr_db``.
    """
    payload = b"\xab\xcd\xef\x01\x02\x03"
    sync_word = 0x12
    return {
        "type": "lora_frame",
        "ts": "2026-04-27T12:00:00+00:00",
        "seq": 1,
        "payload": payload,
        "payload_len": len(payload),
        "crc_valid": True,
        "cr": 4,
        "is_downchirp": False,
        "payload_hash": 0xCAFEF00DDEADBEEF,
        "id": "11111111-1111-1111-1111-111111111111",
        "phy": {
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "sync_word": sync_word,
            "crc_valid": True,
            "snr_db": snr_db,
        },
        "carrier": {
            "sync_word": sync_word,
            "sf": 8,
            "bw": 62500,
            "cr": 4,
            "ldro_cfg": False,
        },
        "source": source,
        "direction": "rx",
    }


# ---------------------------------------------------------------------------
# Synthetic upstream
# ---------------------------------------------------------------------------


class _SyntheticUpstream:
    """A UDP server that periodically replies to a registered subscriber.

    The daemon's UdpSource sends one ``subscribe`` then keepalives
    every 5s; we cache its addr on first subscribe and replay the
    same ``lora_frame`` every ``send_interval_s`` seconds. Periodic
    replay is needed because the daemon broadcasts to whoever is
    subscribed at flush time — the *test* client registers slightly
    later than the daemon's first inbound frame, so a single one-shot
    send would land in an empty registry.
    """

    def __init__(
        self, name: str, snr_db: float, *, send_interval_s: float = 0.1
    ) -> None:
        self.name = name
        self.snr_db = snr_db
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._send_interval_s = send_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_addr: tuple[str, int] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _serve(self) -> None:
        last_send = 0.0
        frame_bytes = cbor2.dumps(
            _make_lora_frame(snr_db=self.snr_db, source=self.name)
        )
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except TimeoutError, OSError:
                pass
            else:
                try:
                    msg = cbor2.loads(data)
                except Exception:
                    msg = None
                if isinstance(msg, dict) and msg.get("type") == "subscribe":
                    with self._lock:
                        self._client_addr = addr
            now = time.monotonic()
            if (
                self._client_addr is not None
                and now - last_send >= self._send_interval_s
            ):
                try:
                    self.sock.sendto(frame_bytes, self._client_addr)
                    last_send = now
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Subscriber helper
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _subscribe_and_receive(daemon_port: int, deadline: float) -> dict[str, Any] | None:
    """Subscribe to the daemon and return the first multi-source frame seen.

    "Multi-source" = ``diversity.n_candidates >= 2``. Single-candidate
    frames may arrive early (e.g. before both upstreams have replied),
    so we skip them and keep listening until both sources line up in
    one aggregator window. Returns ``None`` on deadline.
    """
    sub = cbor2.dumps({"type": "subscribe"})
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    last_send = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_send > 0.3:
                try:
                    sock.sendto(sub, ("127.0.0.1", daemon_port))
                except OSError:
                    pass
                last_send = now
            try:
                data, _addr = sock.recvfrom(65536)
            except TimeoutError, OSError:
                continue
            try:
                msg = cbor2.loads(data)
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("type") != "lora_frame":
                continue
            div = msg.get("diversity")
            if isinstance(div, dict) and div.get("n_candidates", 0) >= 2:
                return msg
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_multi_source_a_b_dedup_and_ranking(tmp_path: Path) -> None:
    """Two upstreams emit same payload at different SNR -> one combined frame.

    Verifies:
    * diversity.n_candidates == 2
    * diversity.rx_sources contains both upstream names
    * phy.snr_db reflects the higher-SNR source (12.0)
    * DuckDB has exactly one row for the synthesised id
    """
    upstream_868 = _SyntheticUpstream("radio_868", snr_db=5.0)
    upstream_2g4 = _SyntheticUpstream("radio_2g4", snr_db=12.0)
    upstream_868.start()
    upstream_2g4.start()

    daemon_port = _free_port()
    db_path = tmp_path / "lora.duckdb"
    identity_dir = tmp_path / "identity"

    config = DaemonConfig(
        listen_host="127.0.0.1",
        listen_port=daemon_port,
        upstreams=(
            UpstreamConfig(
                name="radio_868",
                host="127.0.0.1",
                port=upstream_868.port,
                strict=True,
            ),
            UpstreamConfig(
                name="radio_2g4",
                host="127.0.0.1",
                port=upstream_2g4.port,
                strict=True,
            ),
        ),
        # Tighten the aggregator window so the test wraps fast.
        aggregator_window_ms=40,
        storage=StorageConfig(db_path=db_path),
        identity=IdentityConfig(
            identity_file=identity_dir / "identity.bin",
            keys_dir=identity_dir / "keys",
            channels_dir=identity_dir / "channels",
            contacts_dir=identity_dir / "contacts",
        ),
    )

    received: dict[str, Any] | None = None

    async def _receiver() -> None:
        nonlocal received
        loop = asyncio.get_running_loop()
        # Wait briefly so the daemon's listen socket is up.
        await asyncio.sleep(0.3)
        deadline = time.monotonic() + 4.0
        received = await loop.run_in_executor(
            None, _subscribe_and_receive, daemon_port, deadline
        )
        # Frame in hand — shut the daemon down. SIGTERM lands on the
        # asyncio Lifecycle handler.
        os.kill(os.getpid(), signal.SIGTERM)

    receiver_task = asyncio.create_task(_receiver())

    try:
        rc = await asyncio.wait_for(run_daemon(config), timeout=8.0)
    finally:
        upstream_868.stop()
        upstream_2g4.stop()
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError, BaseException:
            pass

    assert rc == 0, f"daemon exit code {rc}"
    assert received is not None, "no lora_frame received from daemon"

    # ---- Diversity assertions ---------------------------------------
    diversity = received.get("diversity")
    assert isinstance(diversity, dict), f"missing diversity: {received!r}"
    assert diversity["n_candidates"] == 2, diversity
    sources = sorted(diversity.get("rx_sources", []))
    assert sources == ["radio_2g4", "radio_868"], sources

    # ---- Winning SNR ------------------------------------------------
    phy = received.get("phy")
    assert isinstance(phy, dict)
    assert phy.get("snr_db") == 12.0, phy

    # ---- DuckDB single-row dedup ------------------------------------
    # The writer's stop() drains its queue; by the time run_daemon
    # returns the row is committed.
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT COUNT(*) FROM lora_frames WHERE id = ?",
            ["11111111-1111-1111-1111-111111111111"],
        ).fetchall()
    finally:
        con.close()
    assert rows[0][0] == 1, f"expected one row, got {rows[0][0]}"
