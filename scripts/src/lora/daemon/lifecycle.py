#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Asyncio shutdown coordination for the lora-core daemon.

Owns the signal handlers:

* SIGTERM / SIGINT -> set shutdown event; tasks drain and exit
* SIGUSR1          -> faulthandler.dump_traceback (debug aid)
* SIGHUP           -> placeholder until :meth:`install_sighup` upgrades
                       it into the Phase 2F config-reload + key-material
                       hot-swap trigger

The SIGHUP path uses a "set asyncio.Event from the signal handler;
service the event from a long-running task" pattern. Signal handlers
must not call asyncio synchronization primitives or do I/O directly;
the event-driven dispatch keeps the actual reload work on the event
loop's thread.

faulthandler is enabled at first instantiation so a hung process can be
inspected with ``kill -SIGSEGV`` or ``kill -USR1`` without manual
``faulthandler.enable()`` plumbing in main().
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lora.daemon.hot_reload import HotReloader

_log = logging.getLogger("lora.daemon.lifecycle")

# Signals we install handlers for at startup. SIGHUP is included so the
# daemon doesn't die on terminal-disconnect — Phase 2F replaces the
# placeholder with the reload handler.
_SHUTDOWN_SIGNALS: tuple[int, ...] = (
    signal.SIGTERM,
    signal.SIGINT,
)
_DEBUG_SIGNALS: tuple[int, ...] = (
    getattr(signal, "SIGUSR1", -1),  # type: ignore[arg-type]
)
_PLACEHOLDER_SIGNALS: tuple[int, ...] = (
    getattr(signal, "SIGHUP", -1),  # type: ignore[arg-type]
)


class Lifecycle:
    """Owns the daemon's shutdown event and signal handlers."""

    def __init__(self) -> None:
        self._shutdown = asyncio.Event()
        # Enable faulthandler once per process. Idempotent.
        if not faulthandler.is_enabled():  # pragma: no branch
            try:
                faulthandler.enable()
            except RuntimeError, ValueError:  # pragma: no cover - rare
                pass
        self._installed: list[int] = []
        # Phase 2F SIGHUP plumbing — populated only after
        # :meth:`install_sighup` runs.
        self._reload_event: asyncio.Event | None = None
        self._reloader: HotReloader | None = None
        self._reload_task: asyncio.Task[None] | None = None

    # -- signal handlers ----------------------------------------------------

    def install_handlers(self) -> None:
        """Wire signal handlers into the running asyncio loop.

        On non-POSIX platforms (Windows) ``loop.add_signal_handler`` is
        unavailable; we log a warning and fall back to ``KeyboardInterrupt``
        as the only shutdown signal.
        """
        loop = asyncio.get_running_loop()
        if os.name != "posix":  # pragma: no cover - Windows fallback
            _log.warning(
                "non-POSIX platform: signal handlers unavailable; "
                "use Ctrl-C to shut down"
            )
            return
        for sig in _SHUTDOWN_SIGNALS:
            loop.add_signal_handler(sig, self.signal_shutdown)
            self._installed.append(sig)
        for sig in _DEBUG_SIGNALS:
            if sig <= 0:
                continue
            loop.add_signal_handler(sig, self._dump_traceback)
            self._installed.append(sig)
        for sig in _PLACEHOLDER_SIGNALS:
            if sig <= 0:
                continue
            loop.add_signal_handler(sig, self._sighup_placeholder)
            self._installed.append(sig)

    def uninstall_handlers(self) -> None:
        # Cancel the SIGHUP reload-loop task BEFORE removing the
        # signal handler so a final SIGHUP delivered during teardown
        # cannot wake a half-torn-down event.
        task = self._reload_task
        self._reload_task = None
        if task is not None and not task.done():
            task.cancel()
        if os.name != "posix":  # pragma: no cover - Windows fallback
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - test teardown edge
            return
        for sig in self._installed:
            try:
                loop.remove_signal_handler(sig)
            except ValueError, OSError:  # pragma: no cover
                pass
        self._installed.clear()

    # -- public API ---------------------------------------------------------

    def signal_shutdown(self) -> None:
        """Mark shutdown requested. Idempotent."""
        if not self._shutdown.is_set():
            _log.info("shutdown requested")
        self._shutdown.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    async def wait_for_shutdown(self) -> None:
        """Block until :meth:`signal_shutdown` is called."""
        await self._shutdown.wait()

    # -- orphan detection ---------------------------------------------------

    async def watch_parent(
        self,
        *,
        original_ppid: int | None = None,
        poll_interval_s: float = 5.0,
    ) -> None:
        """Trigger shutdown if the parent process disappears.

        When the daemon is spawned by a harness (hwtests, supervisor)
        and the harness crashes without sending SIGTERM, the daemon is
        re-parented to ``init`` (PID 1 on POSIX) and would otherwise
        keep running, holding the DuckDB lock and the listen port. This
        watcher polls ``os.getppid`` and flips :attr:`_shutdown` when
        the parent goes away.

        ``original_ppid`` defaults to the parent at task-creation time.
        Pass an explicit value if the daemon was started via fork+exec
        through a launcher you want to track instead.
        """
        if original_ppid is None:
            original_ppid = os.getppid()
        # PID 1 means re-parented to init — original parent is gone.
        if original_ppid in (0, 1):
            return
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=poll_interval_s
                    )
                    return
                except TimeoutError:
                    pass
                cur_ppid = os.getppid()
                if cur_ppid in (1, 0) or cur_ppid != original_ppid:
                    _log.warning(
                        "parent process gone (ppid %d -> %d); shutting down",
                        original_ppid,
                        cur_ppid,
                    )
                    self.signal_shutdown()
                    return
        except asyncio.CancelledError:
            return

    # -- placeholders -------------------------------------------------------

    def _dump_traceback(self) -> None:
        _log.info("SIGUSR1: dumping tracebacks")
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except OSError, RuntimeError:  # pragma: no cover
            pass

    def _sighup_placeholder(self) -> None:
        _log.info("SIGHUP received; reload handler is Phase 2F (no-op for now)")

    # -- SIGHUP / hot reload (Phase 2F) -----------------------------------

    def install_sighup(self, reloader: HotReloader) -> None:
        """Replace the SIGHUP placeholder with the real reload trigger.

        SIGHUP sets an :class:`asyncio.Event`; a long-running task
        awaits the event and calls :meth:`HotReloader.reload`. The
        signal handler itself does no I/O, no logging, no async work
        — that would race with the loop or violate the
        signal-safety contract.

        Idempotent: calling twice replaces the previous reloader and
        cancels any pending reload task. On non-POSIX platforms (no
        SIGHUP) this is a no-op.
        """
        if os.name != "posix":  # pragma: no cover - Windows fallback
            _log.warning("non-POSIX platform: SIGHUP unavailable; hot reload disabled")
            return
        sig = getattr(signal, "SIGHUP", -1)  # type: ignore[arg-type]
        if sig <= 0:  # pragma: no cover - SIGHUP missing
            return
        loop = asyncio.get_running_loop()

        # Cancel any prior reload task before swapping in the new one.
        prior = self._reload_task
        self._reload_task = None
        if prior is not None and not prior.done():
            prior.cancel()

        self._reloader = reloader
        self._reload_event = asyncio.Event()

        # Replace whatever sat on SIGHUP (placeholder or older
        # install_sighup wiring). add_signal_handler overwrites by
        # default but we go via remove first for symmetry with the
        # uninstall path.
        try:
            loop.remove_signal_handler(sig)
        except ValueError, NotImplementedError:  # pragma: no cover
            pass
        loop.add_signal_handler(sig, self._sighup_set_event)
        if sig not in self._installed:
            self._installed.append(sig)

        self._reload_task = asyncio.create_task(
            self._reload_loop(), name="lora-hot-reload"
        )

    def _sighup_set_event(self) -> None:
        """Signal-handler-safe: just toggles the event flag."""
        ev = self._reload_event
        if ev is not None:
            ev.set()

    async def _reload_loop(self) -> None:
        """Background task: await the reload event, run reload, repeat."""
        ev = self._reload_event
        reloader = self._reloader
        if ev is None or reloader is None:  # pragma: no cover - guard
            return
        while not self._shutdown.is_set():
            shutdown_wait = asyncio.create_task(self._shutdown.wait())
            event_wait = asyncio.create_task(ev.wait())
            try:
                done, pending = await asyncio.wait(
                    [shutdown_wait, event_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:  # pragma: no cover
                shutdown_wait.cancel()
                event_wait.cancel()
                raise
            for t in pending:
                t.cancel()
            if self._shutdown.is_set():
                return
            if not ev.is_set():
                continue
            ev.clear()
            try:
                result = reloader.reload()
            except Exception:  # noqa: BLE001 — reloader.reload swallows
                _log.exception("hot reload raised")
                continue
            _log.info(
                "hot reload: applied=%s refused=%s errors=%s",
                result.applied,
                [f for f, _ in result.refused],
                result.errors,
            )
