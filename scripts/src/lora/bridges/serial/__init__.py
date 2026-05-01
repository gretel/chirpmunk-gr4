#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""DTR-safe serial-to-TCP bridge for the MeshCore companion firmware.

Phase 4 split: this is the verbatim port of ``serial_bridge.py``
into the canonical package layout, plus a :func:`main` entrypoint wired
through the ``lora bridge serial`` CLI subcommand.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import serial

try:
    import setproctitle
except ImportError:  # pragma: no cover
    setproctitle = None  # type: ignore[assignment]

log = logging.getLogger("lora.bridges.serial")


async def _run(args: argparse.Namespace) -> None:
    ser = serial.Serial()
    ser.port = args.serial
    ser.baudrate = args.baud
    ser.timeout = 0.01
    ser.dtr = False
    ser.rts = False
    ser.open()
    log.info("serial: %s @ %d (DTR/RTS suppressed)", args.serial, args.baud)

    loop = asyncio.get_event_loop()

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("client connected: %s", peer)

        async def serial_to_tcp() -> None:
            while not reader.at_eof():
                data = await loop.run_in_executor(
                    None, lambda: ser.read(ser.in_waiting or 1)
                )
                if data:
                    writer.write(data)
                    await writer.drain()
                else:
                    await asyncio.sleep(0.001)

        async def tcp_to_serial() -> None:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                ser.write(data)

        t1 = asyncio.create_task(serial_to_tcp())
        t2 = asyncio.create_task(tcp_to_serial())
        _done, pending = await asyncio.wait(
            [t1, t2], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        writer.close()
        log.info("client disconnected: %s", peer)

    server = await asyncio.start_server(handle_client, args.host, args.tcp_port)
    log.info("bridge: TCP:%d <-> %s", args.tcp_port, args.serial)
    try:
        await server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


def main(argv: list[str] | None = None) -> int:
    """``lora bridge serial`` entrypoint."""
    if setproctitle is not None:
        setproctitle.setproctitle("serial-bridge")
    parser = argparse.ArgumentParser(
        prog="lora bridge serial",
        description="DTR-safe serial-to-TCP bridge for the MeshCore companion",
    )
    parser.add_argument(
        "--serial", default="/dev/cu.usbserial-0001", help="serial port"
    )
    parser.add_argument("--baud", type=int, default=115200, help="baud rate")
    parser.add_argument("--tcp-port", type=int, default=7835, help="TCP listen port")
    parser.add_argument("--host", default="127.0.0.1", help="TCP listen address")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
