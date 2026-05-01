# SPDX-License-Identifier: ISC
"""Property-based fuzz tests for lora.core.schema.

These exist primarily to enforce one invariant: ``dispatch`` MUST NOT
raise on arbitrary input. The daemon's hot path drops failures
silently; any uncaught exception inside :func:`dispatch` would crash
the source loop. Hypothesis hammers this with random nested
dictionaries to catch regressions.

Secondary check: round-trip of randomly-generated valid lora_frame
maps preserves the typed dataclass contract.
"""

from __future__ import annotations

import math

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lora.core.schema import (
    KNOWN_BWS,
    dispatch,
    validate_lora_frame,
    validate_phy,
)

# ---- arbitrary-input fuzz ------------------------------------------------


_arbitrary_value = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**64 - 1)
    | st.floats()
    | st.text()
    | st.binary(),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(), children, max_size=5)
    ),
    max_leaves=20,
)


@given(_arbitrary_value)
@settings(
    max_examples=400,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_dispatch_never_raises(d):
    """``dispatch`` must swallow every possible bad input shape."""
    result = dispatch(d)  # type: ignore[arg-type]
    # Result is either None (rejection) or a typed dataclass.  Either
    # way no exception escapes.
    assert result is None or hasattr(result, "type")


@given(st.text())
def test_dispatch_string_input_returns_none(s):
    assert dispatch(s) is None  # type: ignore[arg-type]


@given(st.lists(_arbitrary_value, max_size=10))
def test_dispatch_list_input_returns_none(lst):
    assert dispatch(lst) is None  # type: ignore[arg-type]


# ---- valid-frame round-trip ---------------------------------------------


@st.composite
def _valid_lora_frame_dict(draw):
    sf = draw(st.integers(min_value=5, max_value=12))
    bw = draw(st.sampled_from(sorted(KNOWN_BWS)))
    cr = draw(st.integers(min_value=1, max_value=4))
    sw = draw(st.integers(min_value=0, max_value=255))
    payload = draw(st.binary(min_size=0, max_size=255))
    seq = draw(st.integers(min_value=0, max_value=2**32 - 1))
    payload_hash = draw(st.integers(min_value=0, max_value=2**64 - 1))
    return {
        "type": "lora_frame",
        "ts": "2026-04-27T12:00:00.000Z",
        "seq": seq,
        "payload": payload,
        "payload_len": len(payload),
        "crc_valid": draw(st.booleans()),
        "cr": cr,
        "is_downchirp": draw(st.booleans()),
        "payload_hash": payload_hash,
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "phy": {
            "sf": sf,
            "bw": bw,
            "cr": cr,
            "sync_word": sw,
            "crc_valid": True,
        },
        "carrier": {
            "sync_word": sw,
            "sf": sf,
            "bw": bw,
            "cr": cr,
            "ldro_cfg": False,
        },
    }


@given(_valid_lora_frame_dict())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_valid_lora_frame_round_trip(d):
    f = validate_lora_frame(d)
    assert f.payload == d["payload"]
    assert f.phy.sf == d["phy"]["sf"]
    assert f.phy.bw == d["phy"]["bw"]
    assert f.payload_len == len(d["payload"])


# ---- numeric edge cases (NaN / Inf / out-of-range) -----------------------


def test_phy_rejects_nan_snr():
    """Validator must reject non-finite floats explicitly."""
    bad = {
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "sync_word": 0x12,
        "crc_valid": True,
        "snr_db": float("nan"),
    }
    # Direct call raises; dispatch swallows.
    import pytest

    from lora.core.schema import SchemaError

    with pytest.raises(SchemaError):
        validate_phy(bad)


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_phy_accepts_or_rejects_arbitrary_snr(snr):
    """For any float input, validator either accepts a finite, in-range
    value or raises SchemaError. Never silently accepts NaN/Inf."""
    base = {
        "sf": 8,
        "bw": 62500,
        "cr": 4,
        "sync_word": 0x12,
        "crc_valid": True,
        "snr_db": snr,
    }
    try:
        p = validate_phy(base)
    except Exception as exc:
        from lora.core.schema import SchemaError

        # Only SchemaError is permitted; bare ValueErrors / TypeErrors
        # would be a regression.
        assert isinstance(exc, SchemaError)
        return
    assert p.snr_db is not None
    assert math.isfinite(p.snr_db)
    assert -200 <= p.snr_db <= 200
