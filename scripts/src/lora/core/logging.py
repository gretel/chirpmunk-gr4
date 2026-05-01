#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Coloured stderr logging shared across the lora package.

A small wrapper over :mod:`logging.Formatter` that emits

    HH:MM:SS.mmmm  name  level  message

with ANSI colour on the level field when stderr is a TTY, and falls
back to plain text on pipes / files / syslog. Honours
`NO_COLOR <https://no-color.org/>`_ and ``FORCE_COLOR`` per the Python
3.14 convention.

The module is intentionally minimal: no rotating file handler, no
JSON/CBOR mode, no syslog plumbing. Callers that need richer pipelines
swap the handler attached to the root logger.

Namespace
---------
All loggers in the lora package live under the ``lora.*`` tree,
mirroring the module path::

    lora.daemon.*       — long-running daemon (run_daemon, fanout, …)
    lora.bridges.*      — meshcore / serial companion bridges
    lora.identity.*     — IdentityStore + watcher
    lora.storage.*      — DuckDBWriter and friends
    lora.viz.*          — visualisation tools (mon, waterfall, spectrum)
    lora.tools.*        — one-shot CLIs (wav, meshcore_tx, migrate_*)
    lora.core.*         — shared library code (config, crypto, …)

External per-module level overrides become possible later as
``--log-level=DEBUG:lora.daemon`` or env vars; currently only the root
level is configurable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import IO, Any

#: Default log format (used by the plain-text path; the colour path
#: composes its own prefix).
DEFAULT_LOG_FORMAT = "%(asctime)s  %(name)s  %(levelname)s  %(message)s"

#: Timestamp is handled by :meth:`_ColorFormatter.formatTime` (UTC ms + Z).
#: ``None`` means the formatter produces its own timestamp string.
DEFAULT_LOG_DATEFMT: str | None = None


def _want_color(stream: IO[Any]) -> bool:
    """Determine whether to use ANSI colour on *stream*.

    Priority: ``NO_COLOR`` (off) > ``FORCE_COLOR`` (on) > ``isatty()``.
    """
    if os.environ.get("NO_COLOR", "") != "":
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class _ColorFormatter(logging.Formatter):
    """Log formatter with ANSI colours keyed on log level.

    Timestamps are local time HH:MM:SS.mmmm (4 fractional digits, 100µs
    precision). Level labels are padded full words (e.g. "info   ",
    "warning"). The label is bold+coloured; timestamp, logger name, and
    message are plain.

    Falls back to plain text when *use_color* is ``False``.
    """

    # ANSI SGR codes
    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _BOLD = "\033[1m"

    # Per-level: (colour code, padded full-word label).
    _LEVEL_STYLE: dict[int, tuple[str, str]] = {
        logging.DEBUG: ("\033[36m", "debug  "),
        logging.INFO: ("\033[32m", "info   "),
        logging.WARNING: ("\033[33m", "warning"),
        logging.ERROR: ("\033[31m", "error  "),
        logging.CRITICAL: ("\033[1;31m", "critical"),
    }
    _DEFAULT_STYLE = ("\033[37m", "???????")

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        use_color: bool = True,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._use_color = use_color

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return local time HH:MM:SS with 4 fractional digits (100 µs precision)."""
        import datetime as _dt

        now = _dt.datetime.fromtimestamp(record.created)
        return now.strftime("%H:%M:%S") + f".{now.microsecond // 100:04d}"

    def format(self, record: logging.LogRecord) -> str:
        if not self._use_color:
            return super().format(record)

        ts = self.formatTime(record)
        color, label = self._LEVEL_STYLE.get(record.levelno, self._DEFAULT_STYLE)
        name = record.name
        msg = record.getMessage()

        extra = ""
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            extra = "\n" + record.exc_text

        prefix = (
            f"{self._DIM}{ts}{self._RESET}  "
            f"{self._DIM}{name}{self._RESET}  "
            f"{color}{self._BOLD}{label}{self._RESET}"
        )

        lines = msg.split("\n")
        first = f"{prefix}  {lines[0]}"
        if len(lines) == 1:
            return first + extra

        pad = " " * (len(ts) + 2 + len(name) + 2 + len(label) + 2)
        rest = "\n".join(f"{pad}{line}" for line in lines[1:])
        return first + "\n" + rest + extra


def setup_logging(
    name: str,
    *,
    log_level: str = "INFO",
    no_color: bool = False,
) -> logging.Logger:
    """Configure stderr logging and return a named logger.

    *log_level* sets the root logger level. ANSI colour is emitted only
    when stderr is a TTY (or ``FORCE_COLOR`` is set, and ``NO_COLOR`` is
    not). Pipes / files / syslog get plain text.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    if no_color:
        use_color = False
    else:
        use_color = _want_color(sys.stderr)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _ColorFormatter(
            fmt=DEFAULT_LOG_FORMAT,
            datefmt=DEFAULT_LOG_DATEFMT,
            use_color=use_color,
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Single-entry-point scripts: replace any existing handlers.
    root.handlers = [handler]

    return logging.getLogger(name)


def add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Add the standard ``--log-level`` and ``--no-color`` arguments."""
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default="INFO",
        choices=[
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "debug",
            "info",
            "warning",
            "error",
        ],
        help="Log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI colour output (also: NO_COLOR env var)",
    )
