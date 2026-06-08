#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""``lora daemon`` — foreground supervisor for long-running lora subcommands.

Reads ``[startup].services`` from ``apps/config.toml``.  The ``core``
service runs **in-process** as an asyncio task (importing
:func:`lora.daemon.main.run_daemon` directly).  All other services
(``bridge.meshcore``, ``bridge.serial``, ``wav``) are spawned as child
subprocesses via ``asyncio.create_subprocess_exec``.

Supervision is **fail-fast**: first child to exit (any rc) or first
in-process error → SIGTERM siblings → grace → SIGKILL → supervisor
exits with the failed child's rc.

Operator manages the supervisor's own lifecycle externally (init.d on
Buildroot, ``nohup`` / ``tmux`` on dev, anything on a Linux server) —
the supervisor stays foreground.

Spec: ``docs/superpowers/specs/2026-04-29-lora-daemon-supervisor-design.md``
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal as _signal
import sys
from dataclasses import dataclass
from pathlib import Path

import setproctitle

from lora.core.config import find_config, load_config
from lora.core.logging import setup_logging
from lora.daemon.config import DaemonConfig, SupervisorConfig

_log = logging.getLogger("lora.daemon.supervisor")

#: Time given to children to honour SIGTERM before SIGKILL. Hardcoded;
#: not config-tunable. KISS.
SHUTDOWN_GRACE_S: float = 5.0


@dataclass(frozen=True, slots=True)
class _ServiceSpec:
    """Metadata for a supervisable lora subcommand."""

    argv_suffix: tuple[str, ...]
    #: Whether this subcommand accepts a ``--config PATH`` flag.
    #: ``lora wav`` and ``lora bridge serial`` do not (they consume
    #: their state via UDP / TCP, not the TOML).
    takes_config: bool


#: Whitelist of service names supervisable from ``[startup].services``.
#: Key is the dot-encoded name documented in the spec
#: (``"bridge.meshcore"`` → ``["bridge", "meshcore"]``).
#:
#: Visualisation subcommands (``mon``, ``waterfall``, ``spectrum``)
#: and one-shot subcommands (``tx``, ``hwtest``) are intentionally
#: excluded — they are interactive / single-run, not long-running.
KNOWN_SERVICES: dict[str, _ServiceSpec] = {
    "core": _ServiceSpec(("core",), takes_config=True),
    "bridge.meshcore": _ServiceSpec(("bridge", "meshcore"), takes_config=True),
    "bridge.serial": _ServiceSpec(("bridge", "serial"), takes_config=False),
    "wav": _ServiceSpec(("wav",), takes_config=False),
}


def validate_services(names: tuple[str, ...]) -> None:
    """Raise :class:`ValueError` if any entry is not in :data:`KNOWN_SERVICES`."""
    for name in names:
        if name not in KNOWN_SERVICES:
            raise ValueError(
                f"lora daemon: unknown service {name!r} in [startup].services; "
                f"known: {sorted(KNOWN_SERVICES)}"
            )


def service_argv(name: str, *, lora_argv0: str, config_path: Path) -> list[str]:
    """Build the argv vector for ``asyncio.create_subprocess_exec``.

    ``lora_argv0`` is normally ``sys.argv[0]`` so child processes
    re-enter the same ``lora`` entry point (matters when the user
    runs from a venv without activating it).

    ``--config`` is appended only for services whose
    :attr:`_ServiceSpec.takes_config` is ``True`` — passing it to a
    subcommand that doesn't recognise the flag (``wav``,
    ``bridge.serial``) would crash the child immediately.
    """
    spec = KNOWN_SERVICES[name]
    argv = [lora_argv0, *spec.argv_suffix]
    if spec.takes_config:
        argv += ["--config", str(config_path)]
    return argv


class Supervisor:
    """Run the ``core`` data-plane in-process and supervise child subprocesses.

    ``services`` maps display-name -> argv vector for child
    subprocesses (bridge, wav, etc.).  The ``core`` service is *not*
    included here — it runs in-process via ``core_config``.

    When ``core_config`` is ``None`` only child subprocesses are
    managed (no in-process core).
    """

    def __init__(
        self,
        services: dict[str, list[str]],
        *,
        core_config: DaemonConfig | None = None,
        config_path: Path | None = None,
    ) -> None:
        if not services and core_config is None:
            raise ValueError(
                "Supervisor requires at least one service or in-process core"
            )
        self._services = dict(services)
        self._core_config = core_config
        self._config_path = config_path
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self._core_task: asyncio.Task[int] | None = None
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        """Mark shutdown requested. Called from signal handler or test."""
        self._shutdown.set()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        # Install signal handlers for SIGTERM + SIGINT. SIGHUP is
        # handled inside run_daemon (hot reload) when core is in-process.
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except NotImplementedError:  # Windows
                pass

        # Spawn child subprocesses (bridge.meshcore, wav, etc.).
        for name, argv in self._services.items():
            _log.info("spawn %s argv=%s", name, argv)
            child = await asyncio.create_subprocess_exec(*argv)
            self.children[name] = child
            _log.info("spawn %s pid=%d", name, child.pid)

        # Start in-process core task if configured.
        core_task: asyncio.Task[int] | None = None
        if self._core_config is not None:
            _log.info("core: running in-process")
            core_task = asyncio.create_task(
                self._run_core(), name="lora-core-inprocess"
            )
            self._core_task = core_task

        # Build wait set: every child's wait() task, core task, shutdown.
        wait_tasks: dict[asyncio.Task, str] = {}
        for n, c in self.children.items():
            t = asyncio.create_task(c.wait(), name=f"wait-{n}")
            wait_tasks[t] = f"child:{n}"
        if core_task is not None:
            wait_tasks[core_task] = "core"
        shutdown_task = asyncio.create_task(self._shutdown.wait(), name="shutdown")

        done, pending = await asyncio.wait(
            [*wait_tasks.keys(), shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Determine exit reason.
        exit_rc = 0
        exit_reason: str | None = None
        for t in done:
            if t is shutdown_task:
                exit_reason = "shutdown"
                continue
            label = wait_tasks.get(t, "unknown")
            try:
                rc = t.result()
            except asyncio.CancelledError:
                continue
            except BaseException as exc:
                _log.error("%s failed: %s", label, exc)
                rc = 1
            if exit_reason is None:
                exit_reason = label
                if isinstance(rc, int):
                    exit_rc = rc
                _log.info("%s finished exit_rc=%d", label, exit_rc)

        # Drain: cancel core (if in-process), SIGTERM children.
        await self._drain(exclude=None)

        # Cancel leftover wait tasks.
        for p in pending:
            p.cancel()
        return exit_rc

    async def _run_core(self) -> int:
        """Start ``run_daemon`` and return its exit code.

        Import is deferred so the module tree is loaded inside
        the asyncio event loop (avoid import-time side effects).
        """
        from lora.daemon.main import run_daemon

        assert self._core_config is not None
        try:
            return await run_daemon(self._core_config, config_path=self._config_path)
        except asyncio.CancelledError:
            _log.info("core: cancelled (supervisor drain)")
            return 0

    async def _drain(self, *, exclude: str | None) -> None:
        """Cancel in-process core (if running) and SIGTERM all children."""
        # Cancel in-process core first.
        if self._core_task is not None and not self._core_task.done():
            _log.info("draining: cancelling in-process core")
            self._core_task.cancel()
            try:
                await asyncio.wait_for(self._core_task, timeout=SHUTDOWN_GRACE_S)
            except (TimeoutError, asyncio.CancelledError):
                if self._core_task and not self._core_task.done():
                    _log.warning("core: did not cancel within %.1fs", SHUTDOWN_GRACE_S)

        # SIGTERM child subprocesses.
        survivors = [
            (n, c)
            for n, c in self.children.items()
            if n != exclude and c.returncode is None
        ]
        if not survivors:
            return
        _log.info(
            "draining: SIGTERM %d siblings (grace=%.1fs)",
            len(survivors),
            SHUTDOWN_GRACE_S,
        )
        for _, c in survivors:
            try:
                c.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(
                asyncio.gather(*(c.wait() for _, c in survivors)),
                timeout=SHUTDOWN_GRACE_S,
            )
        except TimeoutError:
            for n, c in survivors:
                if c.returncode is None:
                    _log.warning("%s SIGKILLed (grace exceeded)", n)
                    try:
                        c.kill()
                    except ProcessLookupError:
                        pass
            await asyncio.gather(
                *(c.wait() for _, c in survivors), return_exceptions=True
            )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lora daemon",
        description="Foreground supervisor for long-running lora subcommands.",
    )
    parser.add_argument("--config", default=None, help="path to apps/config.toml")
    parser.add_argument(
        "--log-level",
        default=None,
        help="override supervisor log level (DEBUG/INFO/WARN/ERROR)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setproctitle.setproctitle("lora-daemon")
    args = _parse_args(argv)

    raw_cfg = load_config(args.config)
    sup_cfg = SupervisorConfig.from_toml(raw_cfg)
    validate_services(sup_cfg.services)

    config_path = find_config(args.config)
    if config_path is None:
        print(
            "lora daemon: no config file resolved; --config is effectively required",
            file=sys.stderr,
        )
        return 2

    log_level = (args.log_level or "INFO").upper()
    setup_logging("lora.daemon.supervisor", log_level=log_level)

    # Build DaemonConfig for in-process core if requested.
    core_config: DaemonConfig | None = None
    if "core" in sup_cfg.services:
        core_config = DaemonConfig.from_legacy_toml(raw_cfg)

    # Build subprocess argv for non-core services.
    services = {
        name: service_argv(name, lora_argv0=sys.argv[0], config_path=config_path)
        for name in sup_cfg.services
        if name != "core"
    }

    sup = Supervisor(
        services=services,
        core_config=core_config,
        config_path=config_path,
    )
    active = list(sup_cfg.services)
    _log.info(
        "lora-daemon starting (pid=%d, services=%s)",
        os.getpid(),
        active,
    )
    rc = asyncio.run(sup.run())
    _log.info("lora-daemon stopped (rc=%d)", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
