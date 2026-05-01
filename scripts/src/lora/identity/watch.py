#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Polling mtime watcher for the IdentityStore.

Designed for the daemon side, which opts in via
:meth:`lora.identity.IdentityStore.start_watch` and wants to be told
when the bridge mutates the on-disk identity / keys / contacts /
channels stores.

Deliberately uses portable polling (``stat().st_mtime``) rather than
inotify / FSEvents — the on-disk layout is small (a handful of files
per directory) and the default 5 s interval makes per-cycle stat cost
negligible.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger("lora.identity.watch")


class MtimeWatcher:
    """Background thread that polls a list of paths for mtime changes.

    Each path may be a regular file (mtime is the file's mtime) or a
    directory (mtime fingerprint covers every entry's name + mtime, so
    new / deleted files trigger the callback too).

    The callback runs on the watcher thread; it should be cheap and
    must not block. Re-entrancy into the IdentityStore is fine because
    the store's own lock is re-entrant.
    """

    def __init__(
        self,
        paths: Iterable[Path],
        callback: Callable[[], None],
        interval_s: float = 5.0,
    ) -> None:
        self._paths: list[Path] = [Path(p) for p in paths]
        self._callback = callback
        self._interval_s = max(0.01, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fingerprints: dict[Path, object] = {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Seed fingerprints synchronously so the very first change after
        # start() is detected even on fast filesystems.
        for p in self._paths:
            self._fingerprints[p] = self._fingerprint(p)
        t = threading.Thread(target=self._run, name="lora-mtime-watch", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    # -- internals --------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            changed = False
            for p in self._paths:
                fp = self._fingerprint(p)
                if fp != self._fingerprints.get(p):
                    self._fingerprints[p] = fp
                    changed = True
            if changed:
                try:
                    self._callback()
                except Exception:  # noqa: BLE001 — never let watcher die from cb
                    log.exception("mtime watcher callback raised")
            self._stop.wait(self._interval_s)

    @staticmethod
    def _fingerprint(path: Path) -> object:
        """Return a hashable fingerprint of `path`'s current state.

        - Missing path -> ``None`` (lets callback fire when it appears).
        - File -> ``("file", mtime_ns, size)``.
        - Dir -> ``("dir", tuple((name, mtime_ns) for each entry))``.
          Entries are sorted so the fingerprint is order-independent.
        """
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if path.is_dir():
            entries: list[tuple[str, int]] = []
            try:
                for entry in path.iterdir():
                    try:
                        es = entry.stat()
                    except OSError:
                        continue
                    entries.append((entry.name, es.st_mtime_ns))
            except OSError:
                return None
            entries.sort()
            return ("dir", tuple(entries))
        return ("file", st.st_mtime_ns, st.st_size)
