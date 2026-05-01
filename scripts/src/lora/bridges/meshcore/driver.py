#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Wrapper around ``meshcore-cli`` for companion-device control.

Used by the hwtest harness (Phase 5) and any tool that needs to drive a
Heltec V3 (or similar MeshCore companion) over either:

- TCP bridge (default): connects to the ``serial-bridge`` TCP port. No
  DTR-induced reboot. ~0.3 s per command.
- Direct serial (fallback): each ``meshcore-cli`` invocation pulses
  DTR, so the device reboots between commands. Add ``reboot_wait``
  (~14 s) to every call.

The class caches the last set radio params to skip redundant ``set
radio`` calls (which are slow on direct-serial mode because of the
reboot).
"""

from __future__ import annotations

import subprocess
import time


class CompanionDriver:
    """Wraps ``meshcore-cli`` for companion-device control.

    Two modes:

      - TCP bridge (default): connects to ``serial-bridge``. No DTR
        reboot, ~0.3 s per command.
      - Direct serial: 14 s reboot per command (fallback).
    """

    def __init__(
        self,
        *,
        bridge_host: str = "127.0.0.1",
        bridge_port: int = 7835,
        serial: str | None = None,
        reboot_wait: float = 14.0,
    ) -> None:
        self.bridge_host = bridge_host
        self.bridge_port = bridge_port
        self.serial = serial
        self.reboot_wait = reboot_wait
        self._last_sf: int | None = None
        self._last_bw: float | None = None

    def _cli_args(self) -> list[str]:
        if self.serial:
            return ["-q", "-s", self.serial]
        return ["-q", "-t", self.bridge_host, "-p", str(self.bridge_port)]

    def _run(self, *args: str, timeout: float = 20.0) -> tuple[bool, str]:
        cmd = ["uvx", "meshcore-cli", *self._cli_args(), *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            ok = r.returncode == 0
            out = r.stdout.strip()
            if not ok or not out:
                # Surface meshcore-cli failures so callers can debug instead of
                # silently treating an empty stdout as "no card".
                import sys

                print(
                    f"meshcore-cli exit={r.returncode} stdout={out!r} "
                    f"stderr={r.stderr.strip()!r}",
                    file=sys.stderr,
                )
            return ok, out
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except FileNotFoundError:
            return False, "uvx not found"

    def set_radio(self, freq_mhz: float, bw_khz: float, sf: int, cr: int = 8) -> bool:
        if self._last_sf == sf and self._last_bw == bw_khz:
            return True
        ok, _ = self._run("set", "radio", f"{freq_mhz},{bw_khz},{sf},{cr}")
        if ok and self.serial:
            time.sleep(self.reboot_wait)
        self._last_sf = sf
        self._last_bw = bw_khz
        return ok

    def set_tx_power(self, dbm: int) -> bool:
        ok, _ = self._run("set", "tx", str(dbm))
        if ok and self.serial:
            time.sleep(self.reboot_wait)
        return ok

    def send_advert(self) -> bool:
        ok, _ = self._run("advert")
        return ok

    def send_msg(self, contact_name: str, text: str) -> tuple[bool, str]:
        """Send a TXT_MSG to a contact by name via ``meshcore-cli``.

        Returns ``(ok, raw_output)`` so callers can inspect ACK status.
        The raw output from ``meshcore-cli`` contains ``"acked"`` when
        the recipient acknowledged the message.
        """
        ok, out = self._run("msg", contact_name, text)
        return ok, out

    def chan_msg(self, channel_name: str, text: str) -> bool:
        """Send a GRP_TXT to a channel by name via ``meshcore-cli``."""
        ok, _ = self._run("chan", channel_name, text)
        return ok

    def get_radio(self) -> str:
        ok, out = self._run("get", "radio")
        return out if ok else ""

    def get_contacts(self) -> str:
        """Return raw output of ``contacts`` command."""
        ok, out = self._run("contacts")
        return out if ok else ""

    def recv_msg(self, timeout: float = 5.0) -> str:
        """Return the next queued message text, or empty string."""
        ok, out = self._run("recv", timeout=timeout)
        return out if ok else ""

    def get_card(self) -> str:
        """Return the node's contact URI (contains pubkey)."""
        ok, out = self._run("card")
        return out if ok else ""
