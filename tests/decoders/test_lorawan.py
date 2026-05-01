# SPDX-License-Identifier: ISC
"""Tests for :class:`lora.decoders.lorawan.LoRaWANDecoder`.

Wire packets are constructed by hand from the LoRaWAN L2 1.0.4 spec
(Section 4) so the test doesn't depend on any encoder implementation.
"""

from __future__ import annotations

from lora.decoders.lorawan import LoRaWANDecoder

from .conftest import make_ctx, make_frame


def _mhdr(mtype: int, major: int = 0) -> bytes:
    return bytes([((mtype & 0x07) << 5) | (major & 0x03)])


def _data_frame(
    *,
    mtype: int,
    dev_addr_le: bytes,
    fctrl: int,
    fcnt_le: bytes,
    fopts: bytes = b"",
    fport: int | None = None,
    frm_payload: bytes = b"",
    mic: bytes = b"\xde\xad\xbe\xef",
) -> bytes:
    parts: list[bytes] = [_mhdr(mtype), dev_addr_le, bytes([fctrl]), fcnt_le, fopts]
    if fport is not None:
        parts.append(bytes([fport]))
        parts.append(frm_payload)
    parts.append(mic)
    return b"".join(parts)


def test_uplink_unconfirmed_data() -> None:
    # MType=2 UnconfDataUp, DevAddr=0x26011BDA (LE), FCtrl=0xC0 (ADR+ADRACKReq),
    # FCnt=42, no FOpts, FPort=1, payload, MIC.
    pkt = _data_frame(
        mtype=2,
        dev_addr_le=b"\xda\x1b\x01\x26",
        fctrl=0xC0,
        fcnt_le=b"\x2a\x00",
        fport=1,
        frm_payload=b"\x01\x02\x03",
        mic=b"\xde\xad\xbe\xef",
    )
    decoder = LoRaWANDecoder()
    ann = decoder.process(make_frame(pkt, sync_word=0x34), make_ctx())
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["mtype"] == "UnconfDataUp"
    assert ann.fields["is_uplink"] is True
    assert ann.fields["is_data"] is True
    assert ann.fields["is_join"] is False
    assert ann.fields["dev_addr"] == "26011bda"
    assert ann.fields["fcnt"] == 42
    assert ann.fields["fport"] == 1
    fctrl = ann.fields["fctrl"]
    assert isinstance(fctrl, dict)
    assert fctrl["adr"] is True
    assert fctrl["adr_ack_req"] is True
    assert fctrl["ack"] is False
    assert fctrl["fpending"] is False  # uplink: bit4 is class_b, fpending is False
    assert fctrl["fopts_len"] == 0
    assert ann.fields["mic"] == b"\xde\xad\xbe\xef"


def test_downlink_confirmed_with_fopts() -> None:
    # MType=5 ConfDataDown, DevAddr=0x01020304, FCtrl=0x32 (ACK+FPending+FOptsLen=2)
    pkt = _data_frame(
        mtype=5,
        dev_addr_le=b"\x04\x03\x02\x01",
        fctrl=0x32,
        fcnt_le=b"\x07\x00",
        fopts=b"\x05\x06",
        fport=2,
        frm_payload=b"\xaa",
        mic=b"\x11\x22\x33\x44",
    )
    decoder = LoRaWANDecoder()
    ann = decoder.process(make_frame(pkt, sync_word=0x34), make_ctx())
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["mtype"] == "ConfDataDown"
    assert ann.fields["is_uplink"] is False
    assert ann.fields["dev_addr"] == "01020304"
    assert ann.fields["fcnt"] == 7
    assert ann.fields["fport"] == 2
    fctrl = ann.fields["fctrl"]
    assert isinstance(fctrl, dict)
    assert fctrl["ack"] is True
    assert fctrl["fpending"] is True
    assert fctrl["fopts_len"] == 2
    assert fctrl["adr_ack_req"] is False  # downlink: not applicable


def test_join_request_marked_join_not_data() -> None:
    # MType=0 JoinRequest, JoinEUI(8) + DevEUI(8) + DevNonce(2) + MIC(4)
    body = b"\x00" * (8 + 8 + 2)
    pkt = _mhdr(0) + body + b"\x99\x88\x77\x66"
    ann = LoRaWANDecoder().process(make_frame(pkt, sync_word=0x34), make_ctx())
    assert ann is not None
    assert ann.ok is True
    assert ann.fields["is_join"] is True
    assert ann.fields["is_data"] is False
    assert ann.fields["is_uplink"] is True
    assert "dev_addr" not in ann.fields  # only data frames carry DevAddr


def test_payload_too_short() -> None:
    ann = LoRaWANDecoder().process(make_frame(b"\x01"), make_ctx())
    assert ann is not None
    assert ann.ok is False
    assert ann.error == "payload_too_short"
