# SPDX-License-Identifier: ISC
"""Smoke test for :mod:`lora.daemon.main` — start, send signal, clean exit."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import threading
from pathlib import Path

import cbor2

from lora.daemon.config import DaemonConfig, UpstreamConfig
from lora.daemon.main import run_daemon
from lora.identity import IdentityConfig
from lora.storage import StorageConfig


def _free_port() -> int:
    """Bind to ephemeral 127.0.0.1 to pick an unused port."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mock_upstream() -> tuple[socket.socket, threading.Event]:
    """Bind a mock UDP upstream that ack-replies subscribe-only.

    Returns the bound socket and a stop event; caller closes the socket
    and sets the event to terminate the responder thread.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.2)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                data, _addr = s.recvfrom(65536)
            except TimeoutError, OSError:
                continue
            try:
                msg = cbor2.loads(data)
            except Exception:
                continue
            del msg  # ignored; the smoke test only needs us to accept subscribe

    threading.Thread(target=_serve, daemon=True).start()
    return s, stop


async def test_main_smoke_starts_and_stops(tmp_path: Path) -> None:
    """Run the daemon for a moment, send SIGTERM, assert clean exit code 0."""
    upstream_sock, upstream_stop = _mock_upstream()
    listen_port = _free_port()
    try:
        identity_dir = tmp_path / "identity"
        config = DaemonConfig(
            listen_host="127.0.0.1",
            listen_port=listen_port,
            upstreams=(
                UpstreamConfig(
                    name="default",
                    host="127.0.0.1",
                    port=upstream_sock.getsockname()[1],
                ),
            ),
            storage=StorageConfig(db_path=tmp_path / "lora.duckdb"),
            identity=IdentityConfig(
                identity_file=identity_dir / "identity.bin",
                keys_dir=identity_dir / "keys",
                channels_dir=identity_dir / "channels",
                contacts_dir=identity_dir / "contacts",
            ),
        )

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_trigger_shutdown())
        rc = await asyncio.wait_for(run_daemon(config), timeout=5.0)
    finally:
        upstream_stop.set()
        upstream_sock.close()
    assert rc == 0
    # DB file should exist after a clean stop (writer.start() created it).
    assert (tmp_path / "lora.duckdb").exists()


async def test_main_returns_2_with_no_upstreams(tmp_path: Path) -> None:
    config = DaemonConfig(
        listen_host="127.0.0.1",
        listen_port=_free_port(),
        upstreams=(),
        storage=StorageConfig(db_path=tmp_path / "lora.duckdb"),
        identity=IdentityConfig(
            identity_file=tmp_path / "id" / "identity.bin",
            keys_dir=tmp_path / "id" / "keys",
            channels_dir=tmp_path / "id" / "channels",
            contacts_dir=tmp_path / "id" / "contacts",
        ),
    )
    rc = await run_daemon(config)
    assert rc == 2
