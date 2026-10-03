# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for canonical JSON, digests and signal fingerprints."""

from __future__ import annotations

import hashlib
import importlib.util
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from datalab_capsule.integrity import (
    CanonicalJSONError,
    canonical_json,
    signal_fingerprint,
    signal_state_facts,
)

GOLDEN = Path(__file__).parent / "golden"
SIGNAL_FINGERPRINT = (
    "sha256:a6fd76dfcb9412db706ad7dbfd3fc68dd4ed57ae0de5032ead395b1ccde4e2ac"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_golden", GOLDEN / "check_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_vectors() -> None:
    """Every golden vector matches in CPython."""
    assert _load_checker().run(str(GOLDEN / "vectors.json")) == []


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf"), {1: "a"}, "\ud800", 2**53 + 1, b"x"],
)
def test_canonical_json_rejects(value) -> None:
    """Non-finite floats, non-string keys, lone surrogates and unsafe ints fail."""
    with pytest.raises(CanonicalJSONError):
        canonical_json(value)


def test_canonical_json_scalars() -> None:
    """Literals, integers and control characters follow RFC 8785."""
    assert (
        canonical_json([None, True, False, 1, -0.0, 1.0]) == "[null,true,false,1,0,1]"
    )
    assert canonical_json("\x1f\u2028é") == '"\\u001f\u2028é"'


def test_fingerprint_matches_independent_payload() -> None:
    """The fingerprint equals a hand-built descriptor and little-endian payload."""
    x = [0.0, 0.25, 0.5, 0.75]
    y = [-2.0, 0.0, 1.0, 4.0]
    descriptor = (
        b'{"dtype":"<f8","length":4,"rows":["x","y"],'
        b'"scheme":"datalab-signal-v1","units":{"x":"s","y":""}}'
    )
    payload = descriptor + b"\x00" + struct.pack("<4d", *x) + struct.pack("<4d", *y)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    big_endian = np.array(y, dtype=">f8")
    assert signal_fingerprint(np.array(x), big_endian, xunit="s") == expected
    assert signal_fingerprint(np.array(x), np.array(y), xunit="s", yunit=None) == (
        expected
    )


def test_fingerprint_rows_units_and_bits() -> None:
    """Units, uncertainty rows and signed zeros change the fingerprint."""
    x = np.array([0.0, 1.0])
    y = np.array([0.0, 2.0])
    base = signal_fingerprint(x, y)
    assert signal_fingerprint(x, y, xunit="s") != base
    assert signal_fingerprint(x, np.array([-0.0, 2.0])) != base
    assert signal_fingerprint(x, y, dy=np.array([np.nan, np.nan])) == base
    assert signal_fingerprint(x, y, dy=np.array([0.1, 0.1])) != base
    with pytest.raises(ValueError):
        signal_fingerprint(x, np.array([1.0, 2.0, 3.0]))
    with pytest.raises(ValueError):
        signal_fingerprint(x, y.astype(np.float32))


def _signal(**kwargs) -> SimpleNamespace:
    values = {
        "x": np.array([0.0, 0.25, 0.5, 0.75]),
        "y": np.array([-2.0, 0.0, 1.0, 4.0]),
        "dx": None,
        "dy": None,
        "xunit": "s",
        "yunit": "",
        "roi": None,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_signal_state_facts() -> None:
    """Plain signals are fingerprinted; ROI, uncertainty and complex are not."""
    facts = signal_state_facts(_signal())
    assert facts["fingerprint"] == {
        "scheme": "datalab-signal-v1",
        "value": SIGNAL_FINGERPRINT,
    }
    assert facts["dtype"] == "float64" and facts["length"] == 4
    assert facts["rows"] == ["x", "y"] and facts["limits"] == []
    assert signal_state_facts(_signal(roi=object()))["limits"] == ["roi"]
    facts = signal_state_facts(_signal(dy=np.ones(4)))
    assert facts["fingerprint"] is None and facts["rows"] == ["x", "y", "dy"]
    complex_y = np.array([1j, 0, 0, 0])
    assert signal_state_facts(_signal(y=complex_y))["limits"] == ["complex_dtype"]
