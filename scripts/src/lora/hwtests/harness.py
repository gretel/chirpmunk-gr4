#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Hardware-test process lifecycle, event collection, and MeshCore companion.

Carved out of the legacy ``lora_test.py`` in Phase 5. Holds
the side-effectful glue that every mode shares:

* Timing constants (``SETTLE_S``, ``FLUSH_*``, port numbers, ``MIN_RATIO``).
* Process-management helpers (:func:`start_process`, :func:`stop_process`)
  and convenience spawners for ``serial_bridge`` / ``lora_trx`` /
  ``lora_scan`` / ``lora_agg`` / ``meshcore_bridge``.
* Readiness probes (:func:`wait_tcp`, :func:`wait_udp`).
* :class:`EventCollector` — background CBOR/UDP drain into a thread-safe
  list with optional type filtering.
* :class:`MeshCoreCompanion` — direct ``meshcore_py`` connection (TX +
  bridge modes) with subscriptions for ``RX_LOG_DATA``, ``ADVERTISEMENT``,
  ``CONTACT_MSG_RECV`` and a serial/TCP reconnect path for the Heltec
  reboot-on-set-radio quirk.

The ``--attach`` flag is implemented at the spawner level: callers pass
``attach=True`` and the spawner returns ``None`` instead of starting a
process. The lifecycle helpers ignore ``None`` cleanly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cbor2

from lora.bridges.meshcore.driver import CompanionDriver
from lora.hwtests.matrix import ConfigPoint
from lora.hwtests.report import err, info

# ---- Timing constants ------------------------------------------------------

SETTLE_S = 2.0  # wait after radio change for PLL lock
FLUSH_DECODE_S = 5.0  # wait after TX for decode pipeline
FLUSH_SCAN_S = 8.0  # wait after TX for scan sweeps (~14 cycles at 555 ms)
FLUSH_SCAN_TUNING_S = 120.0  # tuning scanner: 2 full sweeps at ~60 s each
FLUSH_TX_S = 3.0  # wait after SDR TX for Heltec to receive
FLUSH_BRIDGE_S = 5.0  # wait for bridge RX path (decode + companion protocol)

BRIDGE_PORT = 7835  # serial_bridge TCP port
MESHCORE_BRIDGE_PORT = 7834  # meshcore_bridge TCP port
TRX_PORT = 5556  # lora_trx UDP port (also accepts lora_tx)
AGG_PORT = 5555  # lora-core consumer-facing UDP port
SCAN_PORT = 5557  # lora_scan UDP port

MIN_RATIO = 8.0  # scan detection threshold
FREQ_TOL_HZ = 200_000.0  # accept events within ±200 kHz of TX freq

#: Repository root resolved from this file's location.
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---- Process lifecycle -----------------------------------------------------


# ---------------------------------------------------------------------------
# Spawned-process tracker
# ---------------------------------------------------------------------------
# Every spawn goes through :func:`start_process`, which registers the new
# Popen in :data:`_LIVE_PROCS`. :func:`stop_process` deregisters on success.
# On harness exit (normal OR crash OR SIGTERM/SIGINT) we walk the registry
# and SIGINT every still-alive child's process group, escalating to SIGKILL
# after a timeout. This eliminates the leaked-daemon class of issues that
# left a stale lora-core holding the DuckDB lock between hwtest runs.

_LIVE_PROCS: list[subprocess.Popen[bytes]] = []
#: id(proc) -> (role, log_path). Populated by :func:`start_process`,
#: cleared by :func:`stop_process`. Allows :func:`assert_alive` to
#: print a role-tagged crash banner without callers repeating themselves.
_PROC_LOGS: dict[int, tuple[str, str]] = {}
_cleanup_installed = False


def _cleanup_all_spawned() -> None:
    """atexit / signal-handler entry point: stop every tracked child."""
    # Iterate over a snapshot — stop_process mutates the list.
    for proc in list(_LIVE_PROCS):
        try:
            stop_process(proc)
        except Exception:  # pragma: no cover - best-effort teardown
            pass


def _install_cleanup_handlers() -> None:
    """Register atexit + SIGTERM/SIGINT handlers exactly once.

    Idempotent: subsequent calls are no-ops. Signal handlers re-raise
    after running cleanup so Python's default behaviour (exit code 130
    for SIGINT etc.) is preserved.
    """
    global _cleanup_installed
    if _cleanup_installed:
        return
    import atexit

    atexit.register(_cleanup_all_spawned)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            prior = signal.getsignal(sig)

            def _handler(signum, frame, _prior=prior):  # type: ignore[no-untyped-def]
                _cleanup_all_spawned()
                # Restore default behaviour and re-raise so Python exits
                # with the conventional 128+signum status code.
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

            signal.signal(sig, _handler)
        except OSError, ValueError:  # pragma: no cover - SIGHUP on win32
            continue
    _cleanup_installed = True


def start_process(
    cmd: list[str], log_path: str, *, role: str = "child"
) -> subprocess.Popen[bytes]:
    """Spawn ``cmd`` with stderr redirected to ``log_path`` and own pgrp.

    The new process group lets :func:`stop_process` kill the entire
    subtree (e.g. lora_trx + UHD threads) with one ``killpg``. The
    returned ``Popen`` is registered in the tracker so a harness crash
    or SIGTERM will still take the children down with us.

    ``role`` (e.g. ``"lora_trx"``, ``"lora-core"``) is recorded so
    :func:`assert_alive` can produce a role-tagged crash banner.
    """
    _install_cleanup_handlers()
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    log_fd = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — owned by child
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=log_fd,
        preexec_fn=os.setpgrp,
    )
    _LIVE_PROCS.append(proc)
    _PROC_LOGS[id(proc)] = (role, log_path)
    return proc


def stop_process(proc: subprocess.Popen[bytes] | None) -> None:
    """Send SIGINT to the whole pgrp, escalate to SIGKILL on timeout.

    Removes ``proc`` from the spawn tracker on success.
    """
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass
    try:
        _LIVE_PROCS.remove(proc)
    except ValueError:
        pass
    _PROC_LOGS.pop(id(proc), None)


def assert_alive(
    proc: subprocess.Popen[bytes] | None,
    *,
    role: str | None = None,
    log_path: str | None = None,
) -> None:
    """Abort with a role-tagged log pointer if ``proc`` has exited.

    No-op when ``proc`` is None (``--attach`` mode) or still running.
    On dead child: logs ``<role> died (exit=N); see <log_path>``,
    deregisters the proc, and ``sys.exit(1)``.

    ``role`` / ``log_path`` defaults come from the spawn-time record
    in :data:`_PROC_LOGS`. The keyword arguments override only when a
    caller wants to relabel a specific check (rare).
    """
    if proc is None:
        return
    rc = proc.poll()
    if rc is None:
        return
    role_, log_ = _PROC_LOGS.get(id(proc), ("unknown", "?"))
    err(f"{role or role_} died (exit={rc}); see {log_path or log_}")
    try:
        _LIVE_PROCS.remove(proc)
    except ValueError:
        pass
    _PROC_LOGS.pop(id(proc), None)
    sys.exit(1)


def assert_all_alive() -> None:
    """Run :func:`assert_alive` against every tracked child.

    Cheap (one ``waitpid(WNOHANG)`` per child). Call between matrix
    points and after every long flush sleep so a crashed binary is
    surfaced within ~1 s instead of being masked as a 0/N RF failure.
    """
    for proc in list(_LIVE_PROCS):
        assert_alive(proc)


# ---- Readiness probes ------------------------------------------------------


def wait_tcp(port: int, timeout: float = 15.0, host: str = "127.0.0.1") -> bool:
    """Return True once a TCP connect to ``host:port`` succeeds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=2.0)
            s.close()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def wait_udp(port: int, timeout: float = 30.0, host: str = "127.0.0.1") -> bool:
    """Return True once a CBOR ``subscribe`` to ``host:port`` echoes back.

    Re-sends the subscribe every 5 s so a slow startup eventually picks
    us up. Same socket throughout = same ephemeral port (matches the
    legacy harness behaviour).
    """
    sub_msg = cbor2.dumps({"type": "subscribe"})
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    sock.bind(("", 0))
    deadline = time.monotonic() + timeout
    next_sub = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_sub:
                sock.sendto(sub_msg, (host, port))
                next_sub = now + 5.0
            try:
                sock.recvfrom(65536)
                return True
            except TimeoutError:
                continue
            except Exception:
                time.sleep(1.0)
    finally:
        sock.close()
    return False


# ---- Spawners (compose start_process + module entry points) ----------------


def spawn_serial_bridge(
    serial_port: str,
    *,
    tcp_port: int = BRIDGE_PORT,
    log_path: str = "tmp/bridge.log",
    attach: bool = False,
) -> subprocess.Popen[bytes] | None:
    """Spawn ``lora bridge serial`` and wait for the TCP port. None if attach."""
    if attach:
        return None
    info(f"Starting serial_bridge on {serial_port}")
    proc = start_process(
        [
            sys.executable,
            "-m",
            "lora.bridges.serial",
            "--serial",
            serial_port,
            "--tcp-port",
            str(tcp_port),
        ],
        log_path,
        role="serial_bridge",
    )
    if not wait_tcp(tcp_port):
        err(f"serial_bridge failed (see {log_path})")
        stop_process(proc)
        sys.exit(1)
    info("serial_bridge ready")
    return proc


def spawn_sdr_binary(
    binary: str,
    *,
    config_file: str,
    udp_port: int,
    log_path: str,
    attach: bool = False,
    udp_timeout: float = 30.0,
) -> subprocess.Popen[bytes] | None:
    """Spawn ``lora_trx`` / ``lora_scan`` and wait for UDP CBOR. None if attach."""
    if attach:
        info(f"Attaching to already-running {binary} on port {udp_port}")
        return None
    info(f"Starting {binary}")
    role = os.path.basename(binary) or "sdr_binary"
    proc = start_process([binary, "--config", config_file], log_path, role=role)
    if not wait_udp(udp_port, timeout=udp_timeout):
        err(f"{binary} failed to start (see {log_path})")
        stop_process(proc)
        sys.exit(1)
    info(f"{binary} ready")
    return proc


def spawn_lora_core(
    *,
    config: str | None = None,
    listen: str = f"127.0.0.1:{AGG_PORT}",
    log_path: str = "tmp/lora_core.log",
    attach: bool = False,
) -> subprocess.Popen[bytes] | None:
    """Spawn ``lora core`` (the data-plane daemon).

    Replaces the Phase 1–6 ``lora_agg.py`` shim. Upstream sources come
    from ``[core].upstream`` in ``apps/config.toml``; ``listen`` is the
    consumer-facing UDP address.
    """
    if attach:
        return None
    info("Starting lora-core")
    cmd = [sys.executable, "-m", "lora.cli", "core", "--listen", listen]
    if config is not None:
        cmd += ["--config", config]
    proc = start_process(cmd, log_path, role="lora-core")
    listen_port = int(listen.rsplit(":", 1)[1])
    if not wait_udp(listen_port):
        err(f"lora-core failed (see {log_path})")
        stop_process(proc)
        sys.exit(1)
    info("lora-core ready")
    return proc


def spawn_meshcore_bridge(
    *,
    connect: str = f"127.0.0.1:{AGG_PORT}",
    port: int = MESHCORE_BRIDGE_PORT,
    log_path: str = "tmp/meshcore_bridge.log",
    attach: bool = False,
) -> subprocess.Popen[bytes] | None:
    """Spawn ``lora bridge meshcore`` (companion-protocol bridge)."""
    if attach:
        return None
    info("Starting meshcore-bridge")
    proc = start_process(
        [
            sys.executable,
            "-m",
            "lora.cli",
            "bridge",
            "meshcore",
            "--connect",
            connect,
            "--port",
            str(port),
        ],
        log_path,
        role="meshcore-bridge",
    )
    if not wait_tcp(port):
        err(f"meshcore-bridge failed (see {log_path})")
        stop_process(proc)
        sys.exit(1)
    info("meshcore-bridge ready")
    return proc


# ---- Companion convenience -------------------------------------------------


def companion_apply_and_advert(
    point: ConfigPoint,
    companion: CompanionDriver,
    *,
    tx_repeats: int = 3,
    gap_s: float = 2.0,
    settle_s: float = SETTLE_S,
    pre_send: Callable[[], object] | None = None,
) -> tuple[bool, int]:
    """Configure a MeshCore companion and burst N ADVERTs.

    Steps:
      1. ``set_radio(freq, bw, sf, cr)`` — return ``(False, 0)`` on failure.
      2. ``set_tx_power(point.tx_power)`` — log on failure but continue.
      3. Sleep ``settle_s`` so the PLL locks and the Heltec reboot
         (if direct-serial mode) settles before TX.
      4. Run ``pre_send`` once (e.g. ``EventCollector.drain``) so stale
         receiver events from the retune don't pollute downstream
         collection.
      5. Send ``tx_repeats`` ADVERTs spaced ``gap_s`` seconds apart.

    Returns ``(set_radio_ok, advert_ok_count)``. Shared by ``decode``
    (RX-side validation) and ``transmit`` (TX-only, no SDR) hwtest
    modes. Companion-agnostic — any device that speaks ``meshcore-cli``
    works (Heltec V3, RAK4631, etc.).
    """
    bw_khz = point.bw / 1000.0
    if not companion.set_radio(point.freq_mhz, bw_khz, point.sf, cr=point.cr):
        return False, 0
    if not companion.set_tx_power(point.tx_power):
        err(f"set_tx_power({point.tx_power}) failed")
    time.sleep(settle_s)
    if pre_send is not None:
        pre_send()
    ok = 0
    for i in range(tx_repeats):
        if companion.send_advert():
            ok += 1
        if i < tx_repeats - 1:
            time.sleep(gap_s)
    return True, ok


# ---- CBOR event collector --------------------------------------------------


class EventCollector:
    """Drain CBOR maps from a UDP socket into a thread-safe list.

    The background thread filters by ``event_types`` (None = keep all)
    and silently drops malformed datagrams. Call :meth:`drain` to
    consume the buffer; the underlying socket lifetime is the caller's.
    """

    def __init__(
        self, sock: socket.socket, event_types: set[str] | None = None
    ) -> None:
        self._sock = sock
        self._types = event_types
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._stop.clear()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            out = self._events
            self._events = []
            return out

    def _run(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
                msg = cbor2.loads(data)
                if not isinstance(msg, dict):
                    continue
                if self._types and msg.get("type") not in self._types:
                    continue
                with self._lock:
                    self._events.append(msg)
            except TimeoutError:
                continue
            except Exception:
                continue


# ---- MeshCoreCompanion -----------------------------------------------------


class MeshCoreCompanion:  # pragma: no cover  # meshcore_py wrapper, hw-only
    """Direct connection to a Heltec V3 via ``meshcore_py``.

    Subscribes to ``RX_LOG_DATA`` (rich SNR/RSSI on incoming RF),
    ``ADVERTISEMENT`` (push notifications), and ``CONTACT_MSG_RECV``
    (private messages). Survives the Heltec reboot triggered by
    ``set_radio`` via :meth:`_reconnect`.
    """

    def __init__(self) -> None:
        self._mc: Any = None
        self._serial_port: str = ""
        self._tcp_host: str = ""
        self._tcp_port: int = 0
        self._adverts: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._pubkey: str = ""

    async def connect(
        self,
        serial_port: str | None = None,
        *,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
    ) -> bool:
        from meshcore import MeshCore  # type: ignore[import-not-found]
        from meshcore.events import EventType  # type: ignore[import-not-found]

        if tcp_host is not None and tcp_port is not None:
            self._serial_port = ""
            self._tcp_host = tcp_host
            self._tcp_port = tcp_port
            self._mc = await MeshCore.create_tcp(tcp_host, tcp_port)
        else:
            self._serial_port = serial_port or ""
            self._tcp_host = ""
            self._tcp_port = 0
            self._mc = await MeshCore.create_serial(self._serial_port, 115200)
        if self._mc is None:
            return False
        self._pubkey = self._mc.self_info.get("public_key", "")
        self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rf_packet)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert_push)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
        await self._mc.start_auto_message_fetching()
        return True

    async def _on_rf_packet(self, event: Any) -> None:
        d = event.payload
        if not isinstance(d, dict):
            return
        if d.get("payload_typename") == "ADVERT" or d.get("payload_type") == 4:
            with self._lock:
                self._adverts.append(
                    {
                        "adv_key": d.get("adv_key", ""),
                        "adv_name": d.get("adv_name", ""),
                        "snr": d.get("snr"),
                        "rssi": d.get("rssi"),
                        "source": "rx_log",
                    }
                )

    async def _on_advert_push(self, event: Any) -> None:
        d = event.payload
        if not isinstance(d, dict):
            return
        with self._lock:
            self._adverts.append(
                {
                    "adv_key": d.get("public_key", ""),
                    "adv_name": "",
                    "snr": None,
                    "rssi": None,
                    "source": "push",
                }
            )

    async def _on_message(self, event: Any) -> None:
        d = event.payload
        if not isinstance(d, dict):
            return
        with self._lock:
            self._messages.append(
                {
                    "text": d.get("text", ""),
                    "pubkey_prefix": d.get("pubkey_prefix", ""),
                    "txt_type": d.get("txt_type", 0),
                }
            )

    @property
    def pubkey(self) -> str:
        return self._pubkey

    async def set_radio(
        self, freq_mhz: float, bw_khz: float, sf: int, cr: int = 8
    ) -> bool:
        from meshcore.events import EventType  # type: ignore[import-not-found]

        assert self._mc is not None
        try:
            result = await self._mc.commands.set_radio(
                freq=freq_mhz, bw=bw_khz, sf=sf, cr=cr
            )
            if result.type == EventType.OK:
                return True
            info(f"  set_radio: reconnecting ({result.payload})")
            return await self._reconnect()
        except Exception as e:
            info(f"  set_radio: reconnecting after exception ({e})")
            return await self._reconnect()

    async def _reconnect(self) -> bool:
        from meshcore import MeshCore  # type: ignore[import-not-found]
        from meshcore.events import EventType  # type: ignore[import-not-found]

        try:
            if self._mc is not None:
                await self._mc.disconnect()
        except Exception:
            pass
        await asyncio.sleep(3.0)  # wait for Heltec reboot
        if self._tcp_host:
            self._mc = await MeshCore.create_tcp(self._tcp_host, self._tcp_port)
        else:
            self._mc = await MeshCore.create_serial(self._serial_port, 115200)
        if self._mc is None:
            err("reconnect failed")
            return False
        self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rf_packet)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert_push)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
        await self._mc.start_auto_message_fetching()
        info("  reconnected")
        return True

    async def send_advert(self) -> bool:
        from meshcore.events import EventType  # type: ignore[import-not-found]

        assert self._mc is not None
        try:
            result = await self._mc.commands.send_advert()
            return result.type == EventType.OK
        except Exception as e:
            info(f"  send_advert failed: {e}")
            return False

    async def send_message(self, dest_pubkey: bytes, text: str) -> bool:
        from meshcore.events import EventType  # type: ignore[import-not-found]

        assert self._mc is not None
        try:
            result = await self._mc.commands.send_msg(dest_pubkey, text)
            return result.type in (EventType.OK, EventType.MSG_SENT)
        except Exception as e:
            info(f"  send_message failed: {e}")
            return False

    def drain_adverts(self) -> list[dict[str, Any]]:
        with self._lock:
            out = self._adverts
            self._adverts = []
            return out

    def drain_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            out = self._messages
            self._messages = []
            return out

    async def close(self) -> None:
        if self._mc:
            await self._mc.disconnect()


__all__ = [
    "AGG_PORT",
    "BRIDGE_PORT",
    "EventCollector",
    "FLUSH_BRIDGE_S",
    "FLUSH_DECODE_S",
    "FLUSH_SCAN_S",
    "FLUSH_SCAN_TUNING_S",
    "FLUSH_TX_S",
    "FREQ_TOL_HZ",
    "MESHCORE_BRIDGE_PORT",
    "MIN_RATIO",
    "MeshCoreCompanion",
    "REPO_ROOT",
    "SCAN_PORT",
    "SETTLE_S",
    "TRX_PORT",
    "assert_alive",
    "assert_all_alive",
    "companion_apply_and_advert",
    "spawn_lora_core",
    "spawn_meshcore_bridge",
    "spawn_sdr_binary",
    "spawn_serial_bridge",
    "start_process",
    "stop_process",
    "wait_tcp",
    "wait_udp",
]
