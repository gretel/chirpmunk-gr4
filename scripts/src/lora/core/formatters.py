#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Human-readable formatters for LoRa frames and binary payloads.

Two layers:

  - low-level byte rendering (`format_hex`, `format_hexdump`,
    `format_ascii`, `sanitize_text`, `utf8_truncate`) — terminal-safe;
    escape C0/C1 controls, lone surrogates, bidi overrides, zero-width
    characters, and TAG codepoints.
  - high-level frame rendering (`format_frame`, `format_diversity`,
    `format_protocol`) used by `lora.viz.mon`, `lora.tools.decode`,
    and the legacy decode demos to print one decoded ``lora_frame``
    per record.

`format_frame` accepts injected callables for MeshCore protocol summary
and decryption (legacy callback path) AND will render a pre-attached
``protocol`` sub-map if present (Phase 3 lora-core path). The callback
path stays for `lora.viz.mon` until Phase 7.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .constants import sync_word_name


def format_hex(data: bytes, *, sep: str = " ", max_bytes: int | None = None) -> str:
    """Format bytes as uppercase hex with optional truncation."""
    if max_bytes is not None:
        preview = sep.join(f"{b:02X}" for b in data[:max_bytes])
        if len(data) > max_bytes:
            preview += f" ...({len(data)}B)"
        return preview
    return sep.join(f"{b:02X}" for b in data)


def format_hexdump(
    data: bytes,
    *,
    max_bytes: int = 48,
    indent: str = "  ",
    cols: int = 16,
) -> str:
    """Format bytes as an xxd-style hex dump with ASCII sidebar.

    Each line shows up to *cols* bytes as hex pairs followed by
    printable ASCII (dots for non-printable). Truncates at *max_bytes*
    with an indicator. Returns a multi-line string (no trailing
    newline).
    """
    show = data[:max_bytes]
    lines: list[str] = []
    for off in range(0, len(show), cols):
        chunk = show[off : off + cols]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        # Pad hex part to fixed width so the ASCII sidebar aligns.
        hex_width = cols * 3 - 1
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{indent}{hex_part:<{hex_width}}  {ascii_part}")
    if len(data) > max_bytes:
        lines.append(f"{indent}... ({len(data)} bytes total)")
    return "\n".join(lines)


# Codepoints that are dangerous or invisible when rendered on a
# terminal.  Escaped by `sanitize_text` regardless of context.
_DANGEROUS_CODEPOINTS: frozenset[int] = frozenset(
    # C1 controls (U+0080..U+009F) — includes CSI U+009B which acts
    # as ESC[ on 8-bit terminals.
    set(range(0x80, 0xA0))
    # Lone surrogates (U+D800..U+DFFF) — invalid Unicode, crash on
    # re-encode.
    | set(range(0xD800, 0xE000))
    # Zero-width / bidi / formatting characters.
    | {
        0x200B,  # zero-width space
        0x200C,
        0x200D,  # ZWNJ, ZWJ
        0x200E,
        0x200F,  # LRM, RLM
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,  # bidi embeddings
        0x2066,
        0x2067,
        0x2068,
        0x2069,  # bidi isolates
        0xFEFF,  # BOM / ZWNBSP
        0xFFF9,
        0xFFFA,
        0xFFFB,  # interlinear annotations
    }
    # Unicode TAG characters (U+E0001..U+E007F) — invisible tagging.
    | set(range(0xE0001, 0xE0080))
)


def sanitize_text(text: str) -> str:
    """Remove terminal-hostile characters from a decoded string.

    Escapes C0 controls, DEL, C1 controls (U+0080-U+009F), lone
    surrogates, Unicode bidi overrides, zero-width characters, and TAG
    characters. Safe for printing to any terminal and for embedding in
    log messages.
    """
    parts: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp < 0x20:
            # C0 controls (U+0000..U+001F) — escape all (no TAB / LF
            # exceptions).
            parts.append(f"\\x{cp:02x}")
        elif cp == 0x7F:
            parts.append("\\x7f")
        elif cp in _DANGEROUS_CODEPOINTS:
            parts.append(f"\\u{cp:04x}" if cp < 0x10000 else f"\\U{cp:08x}")
        else:
            parts.append(ch)
    return "".join(parts)


def format_ascii(data: bytes, *, max_bytes: int | None = None) -> str:
    """Decode as UTF-8 where valid, replace invalid bytes with hex escapes.

    All output is console-safe: control characters, C1 controls, bidi
    overrides, and DEL are escaped.
    """
    raw = data[:max_bytes] if max_bytes is not None else data
    parts: list[str] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b < 0x20:
            parts.append(f"\\x{b:02x}")
            i += 1
        elif b == 0x7F:
            parts.append("\\x7f")
            i += 1
        elif b < 0x80:
            parts.append(chr(b))
            i += 1
        else:
            # Try to decode a multi-byte UTF-8 sequence.
            decoded = False
            for length in (4, 3, 2):
                if i + length <= len(raw):
                    try:
                        ch = raw[i : i + length].decode("utf-8")
                        parts.append(sanitize_text(ch))
                        i += length
                        decoded = True
                        break
                    except UnicodeDecodeError:
                        continue
            if not decoded:
                parts.append(f"\\x{b:02x}")
                i += 1
    s = "".join(parts)
    if max_bytes is not None and len(data) > max_bytes:
        s += "..."
    return s


def utf8_truncate(s: str, max_bytes: int) -> bytes:
    """Encode *s* as UTF-8 and truncate to at most *max_bytes* without
    splitting a multi-byte sequence.

    Returns the truncated ``bytes`` (which may be shorter than
    *max_bytes* if a codepoint was split at the boundary).
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    # Walk back from the cut point to a valid UTF-8 character boundary.
    # Continuation bytes have the pattern 10xxxxxx (0x80..0xBF).
    trunc = encoded[:max_bytes]
    while trunc and (trunc[-1] & 0xC0) == 0x80:
        trunc = trunc[:-1]
    # If the last byte is a leading byte (11xxxxxx) whose sequence was
    # cut, remove it too.
    if trunc and trunc[-1] >= 0xC0:
        trunc = trunc[:-1]
    return trunc


# ---- LoRa frame formatting ----


def format_diversity(div: dict[str, Any]) -> str:
    """Format the diversity sub-map from an aggregated lora_frame as one line."""
    n = div.get("n_candidates", 0)
    gap_us = div.get("gap_us", 0)
    decoded_ch = div.get("decoded_channel")
    snrs = div.get("snr_db", [])
    crc_mask = div.get("crc_mask", 0)

    parts = [f"{n} chain{'s' if n != 1 else ''}"]
    if gap_us > 0:
        parts.append(f"gap={gap_us / 1000:.0f}ms")
    if n > 1 and decoded_ch is not None:
        parts.append(f"ch={decoded_ch} won")
    if n > 1 and snrs:
        snr_str = ", ".join(f"{s:.1f}" for s in snrs)
        parts.append(f"[SNR: {snr_str} dB]")
    # Only show crc_mask when not all candidates passed.
    all_ok_mask = (1 << n) - 1
    if n > 1 and crc_mask != all_ok_mask:
        parts.append(f"crc={bin(crc_mask)}")
    return "div: " + "  ".join(parts)


def format_protocol(proto: Mapping[str, Any] | None) -> tuple[str, str | None]:
    """Render a ``ProtocolAnnotation`` sub-map into ``(summary, decrypt_line)``.

    Returns ``("", None)`` for missing / failed annotations. Used by
    :func:`format_frame` when the lora-core daemon has attached a
    ``protocol`` sub-map upstream — the renderer no longer needs to
    re-parse the payload bytes.

    Output shape (matches the legacy lora_mon look):

      - meshcore TXT:    ``"v1 D/TXT path=2"`` + ``"TXT from abcd: hi"``
      - meshcore ADVERT: ``"v1 F/ADVERT path=0"`` + decrypt line if name
      - meshcore GRP:    ``"v1 F/GRP_TXT path=0"`` + ``"GRP_TXT #ch: hi"``
      - lorawan:         ``"lorawan UnconfDataUp dev=01020304 fcnt=42"``
      - raw:             ``"raw sw=0x12"``
    """
    if not isinstance(proto, Mapping):
        return ("", None)
    if not proto.get("ok", False):
        # Surface decoder errors so they show up next to the hex dump.
        err = proto.get("error")
        if err:
            return (f"{proto.get('name', '?')}: {sanitize_text(str(err))}", None)
        return ("", None)

    name = str(proto.get("name", ""))
    fields_raw = proto.get("fields") or {}
    fields: Mapping[str, Any] = fields_raw if isinstance(fields_raw, Mapping) else {}

    summary = ""
    decrypt_line: str | None = None

    if name == "meshcore":
        version = fields.get("version")
        route = fields.get("route", "?")
        ptype = fields.get("payload_type", "?")
        path_len = fields.get("path_len", 0)
        v_str = f"v{version} " if version is not None else ""
        summary = f"{v_str}{route}/{ptype} path={path_len}"

        sender_name = fields.get("sender_name")
        if sender_name:
            summary += f' "{sanitize_text(str(sender_name))}"'

        text = fields.get("decrypted_text")
        if isinstance(text, str) and text:
            channel = fields.get("channel")
            if channel:
                decrypt_line = (
                    f"GRP_TXT #{sanitize_text(str(channel))}: {sanitize_text(text)}"
                )
            else:
                sender = fields.get("sender_pubkey")
                prefix = ""
                if isinstance(sender, (bytes, bytearray, memoryview)):
                    prefix = f" from {bytes(sender)[:4].hex()}"
                decrypt_line = f"{ptype}{prefix}: {sanitize_text(text)}"
    elif name == "lorawan":
        mtype = fields.get("mtype", "?")
        parts = ["lorawan", str(mtype)]
        dev_addr = fields.get("dev_addr")
        if dev_addr:
            parts.append(f"dev={sanitize_text(str(dev_addr))}")
        fcnt = fields.get("fcnt")
        if fcnt is not None:
            parts.append(f"fcnt={fcnt}")
        fport = fields.get("fport")
        if fport is not None:
            parts.append(f"fport={fport}")
        summary = " ".join(parts)
    elif name == "raw":
        sw = fields.get("sync_word_hex", "")
        summary = f"raw sw={sanitize_text(str(sw))}" if sw else "raw"
    else:
        summary = sanitize_text(name) if name else ""

    return (summary, decrypt_line)


def format_frame(
    msg: dict[str, Any],
    *,
    our_prv: bytes | None = None,
    our_pub: bytes | None = None,
    known_keys: dict[str, bytes] | None = None,
    channels: list[Any] | None = None,
    _parse_summary: Callable[[bytes], str] | None = None,
    _decrypt: Callable[..., str | None] | None = None,
) -> str:
    """Format a single ``lora_frame`` CBOR message as human-readable text.

    Layout (each on its own line):

    1. Header: ``#seq sizeB CRC SF/BW/CR sync [label] [ch=N] SNR NF peak``
    2. Protocol + decrypted content (MeshCore only, via _parse_summary /
       _decrypt)
    3. Hex dump (compact; omitted when decrypted content is shown)
    4. Diversity line (aggregated frames from lora_agg only)

    `_parse_summary` and `_decrypt` are optional callables injected by
    `lora_mon` for MeshCore protocol parsing and decryption. When
    ``None``, those blocks are skipped.
    """
    payload = msg.get("payload", b"")
    # Frame identity lives in `carrier` (sync_word/sf/bw/cr); DSP state
    # in `phy`.
    carrier = msg.get("carrier", {})
    phy = msg.get("phy", {})
    direction = msg.get("direction", "rx")
    crc_ok = msg.get("crc_valid", False)
    crc_str = "CRC_OK" if crc_ok else "CRC_FAIL"
    seq = msg.get("seq", 0)
    cr = carrier.get("cr", 0)
    bw = carrier.get("bw")
    sync_word = carrier.get("sync_word", 0)
    sw_label = sync_word_name(sync_word)
    sf = carrier.get("sf", "?")
    snr_db = phy.get("snr_db")
    snr_db_td = phy.get("snr_db_td")
    noise_floor_db = phy.get("noise_floor_db")
    peak_db = phy.get("peak_db")
    rx_ch = msg.get("rx_channel")
    decode_label = msg.get("decode_label")
    dc = " (downchirp)" if msg.get("is_downchirp") else ""

    # BW field: 62500 -> "62.5k", 125000 -> "125k", etc.
    bw_str = f"BW{bw / 1e3:g}k" if bw is not None else ""

    # Line 1: identity + PHY essentials + signal quality.
    dir_tag = "TX " if direction == "tx" else ""
    sf_bw_cr = f"SF{sf}"
    if bw_str:
        sf_bw_cr += f"/{bw_str}"
    sf_bw_cr += f"/CR4:{4 + cr}"
    header = f"{dir_tag}#{seq} {len(payload)}B {crc_str} {sf_bw_cr} {sw_label}"
    if decode_label:
        header += f" [{sanitize_text(decode_label)}]"
    if rx_ch is not None:
        header += f" ch={rx_ch}"
    if snr_db is not None:
        header += f" SNR={snr_db:.1f}dB"
    if snr_db_td is not None:
        header += f" SNR_td={snr_db_td:.1f}dB"
    if noise_floor_db is not None:
        header += f" NF={noise_floor_db:.1f}dBFS"
    if peak_db is not None:
        header += f" peak={peak_db:.1f}dBFS"
    header += dc

    lines = [header]

    # Line 2: protocol summary + decrypted content.
    # Prefer the daemon-attached `protocol` sub-map (lora-core path).
    # Fall back to legacy callbacks for `lora.viz.mon` which
    # still parses inline.
    proto = msg.get("protocol")
    mc_summary = ""
    decrypt_line: str | None = None
    if isinstance(proto, Mapping):
        mc_summary, decrypt_line = format_protocol(proto)
    else:
        if _parse_summary is not None and sync_word == 0x12 and payload:
            mc_summary = _parse_summary(payload)
        if _decrypt is not None and crc_ok and sync_word == 0x12 and payload:
            decrypt_line = _decrypt(
                payload, our_prv, our_pub, known_keys or {}, channels or []
            )

    if mc_summary and decrypt_line:
        lines.append(f"  {mc_summary} | {decrypt_line}")
    elif mc_summary:
        lines.append(f"  {mc_summary}")
    elif decrypt_line:
        lines.append(f"  {decrypt_line}")

    # Hex dump: show when there's no decrypted content (raw / unknown
    # frames).
    if payload and not decrypt_line:
        lines.append(format_hexdump(payload, max_bytes=48, indent="  "))

    # Diversity line (aggregated frames from lora_agg only).
    div = msg.get("diversity")
    if div:
        lines.append(f"  {format_diversity(div)}")

    return "\n".join(lines)
