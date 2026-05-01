#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""
cbor_stream.py -- CBOR Sequence (RFC 8742) stream reader.

Provides a generator that reads concatenated CBOR items from a binary stream
(pipe, file, or socket) without blocking on incomplete data. This solves the
known issue where cbor2.CBORDecoder(stream).decode() blocks indefinitely on
pipes because cbor2 does greedy/buffered reads internally.

The approach uses a CBOR item length calculator (inspired by microcbor) to
determine exact byte boundaries between concatenated items, then passes
exactly-sized slices to cbor2.loads() for full decoding.

  1. Read available bytes into a bytearray buffer.
  2. Calculate the byte length of the next CBOR item from the buffer header.
  3. If enough bytes are present, slice and decode with cbor2.loads().
  4. If incomplete, read more bytes and retry.
  5. On EOF with empty buffer, terminate.

Usage:
    from cbor_stream import read_cbor_seq

    for item in read_cbor_seq(sys.stdin.buffer):
        print(item)

CBOR item length calculation is derived from RFC 8949 Section 3 (Specification
of the CBOR Encoding). The streaming decoder pattern is inspired by
microcbor (Joel Gustafson, MIT license, https://github.com/joeltg/microcbor).
"""

from __future__ import annotations

import struct
from collections.abc import Generator
from typing import Any, BinaryIO

import cbor2

# Sentinel: need more bytes to determine item length.
_NEED_MORE = -1


def cbor_item_length(buf: bytes | bytearray | memoryview, offset: int = 0) -> int:
    """Calculate the total byte length of one CBOR item starting at offset.

    Returns the number of bytes the item occupies, or _NEED_MORE (-1) if the
    buffer is too short to determine the length.

    This walks the CBOR header structure (RFC 8949 Section 3) without
    decoding values. Handles all major types including nested maps/arrays.
    Does not support indefinite-length items (major type 7, additional
    info 31) since our encoder never produces them.
    """
    avail = len(buf) - offset
    if avail < 1:
        return _NEED_MORE

    initial = buf[offset]
    major = initial >> 5
    info = initial & 0x1F
    pos = offset + 1

    # Determine the argument value and how many header bytes it consumed.
    if info < 24:
        arg = info
    elif info == 24:
        if avail < 2:
            return _NEED_MORE
        arg = buf[pos]
        pos += 1
    elif info == 25:
        if avail < 3:
            return _NEED_MORE
        arg = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
    elif info == 26:
        if avail < 5:
            return _NEED_MORE
        arg = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
    elif info == 27:
        if avail < 9:
            return _NEED_MORE
        arg = struct.unpack_from(">Q", buf, pos)[0]
        pos += 8
    else:
        # info 28-30 are reserved; 31 is indefinite-length (unsupported).
        # Treat as 1-byte item (simple value / break) to avoid hanging.
        return 1

    if major in (0, 1):
        # Unsigned/negative integer: header only, no content bytes.
        return pos - offset

    if major in (2, 3):
        # Byte string / text string: header + arg content bytes.
        total = (pos - offset) + arg
        if avail < total:
            return _NEED_MORE
        return total

    if major == 4:
        # Array: header + arg child items.
        for _ in range(arg):
            child = cbor_item_length(buf, pos)
            if child == _NEED_MORE:
                return _NEED_MORE
            pos += child
        return pos - offset

    if major == 5:
        # Map: header + arg key-value pairs (2 * arg child items).
        for _ in range(arg * 2):
            child = cbor_item_length(buf, pos)
            if child == _NEED_MORE:
                return _NEED_MORE
            pos += child
        return pos - offset

    if major == 6:
        # Tagged item: header + one child item.
        child = cbor_item_length(buf, pos)
        if child == _NEED_MORE:
            return _NEED_MORE
        return (pos - offset) + child

    # Major type 7: simple values and floats — header only.
    return pos - offset


def _read_chunk(stream: BinaryIO, chunk_size: int) -> bytes:
    """Read a chunk from stream, using read1() when available.

    BufferedReader.read1() returns as soon as any data is available from a
    single underlying read, which is essential for pipe streaming — it won't
    block waiting to fill the entire chunk_size. Falls back to read() for
    regular files and BytesIO.
    """
    read1 = getattr(stream, "read1", None)
    if read1 is not None:
        return read1(chunk_size)
    return stream.read(chunk_size)


def read_cbor_seq(
    stream: BinaryIO,
    chunk_size: int = 8192,
) -> Generator[Any, None, None]:
    """Yield CBOR items from a CBOR Sequence (RFC 8742) stream.

    Works correctly with pipes (no blocking on incomplete data) and with
    regular files. Each CBOR item is self-delimiting; we use
    cbor_item_length() to find exact byte boundaries, then decode with
    cbor2.loads().

    Args:
        stream: Binary stream to read from (e.g. sys.stdin.buffer, open file).
        chunk_size: Number of bytes to read per chunk (default 8192).

    Yields:
        Decoded CBOR items (dicts, lists, strings, etc.).
    """
    buf = bytearray()

    while True:
        # Try to decode items from whatever we have in the buffer.
        while buf:
            item_len = cbor_item_length(buf)
            if item_len == _NEED_MORE:
                break
            item = cbor2.loads(bytes(buf[:item_len]))
            del buf[:item_len]
            yield item

        # Read more data from the stream.
        chunk = _read_chunk(stream, chunk_size)
        if not chunk:
            # EOF. If there's leftover data in buf, it's a truncated item — discard.
            break
        buf.extend(chunk)


# ---- CBOR config handshake (UDP) ------------------------------------------

#: Default timeout waiting for config message from lora_trx (seconds).
CONFIG_TIMEOUT = 10.0


def wait_for_config(
    sock: "socket.socket",
    *,
    timeout: float = CONFIG_TIMEOUT,
) -> dict[str, Any]:
    """Block until a CBOR ``config`` message arrives on *sock*.

    ``lora_trx`` sends a ``config`` message immediately on subscribe.
    This helper reads datagrams until it receives one with
    ``type == "config"`` or until *timeout* seconds elapse.

    Returns the full decoded CBOR config dict (with ``phy``, ``server``,
    ``rx_chains``, and ``raw`` sub-dicts). The ``raw`` sub-dict mirrors
    the TOML section structure (``network``, ``aggregator``,
    ``meshcore``, ``logging``) and can be passed directly to the
    legacy ``config_*`` accessors in :mod:`lora.core.config`.

    Raises ``TimeoutError`` if no config arrives within the deadline.
    """
    import socket as _socket
    import time as _time

    deadline = _time.monotonic() + timeout
    old_timeout = sock.gettimeout()
    try:
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no config message from lora_trx within {timeout:.0f}s"
                )
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(65536)
            except _socket.timeout:
                raise TimeoutError(
                    f"no config message from lora_trx within {timeout:.0f}s"
                ) from None
            if not data:
                continue
            try:
                msg = cbor2.loads(data)
            except Exception:
                continue
            if isinstance(msg, dict) and msg.get("type") == "config":
                return msg
    finally:
        sock.settimeout(old_timeout)


def apply_config(config_msg: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``raw`` passthrough config from a CBOR config message.

    Returns a dict with the same nested structure as ``config.toml``
    (``network``, ``aggregator``, ``meshcore``, ``logging``). This dict
    works with the legacy ``config_*`` accessor functions in
    :mod:`lora.core.config`.

    Logging configuration is intentionally NOT applied here — log
    level is a CLI concern (``--log-level``, ``--no-color``).
    """
    return config_msg.get("raw", {})


# Re-export the socket type used in :func:`wait_for_config` so that
# callers don't need to remember the local-import dance.
import socket  # noqa: E402  (deliberate late import to keep the top tidy)
