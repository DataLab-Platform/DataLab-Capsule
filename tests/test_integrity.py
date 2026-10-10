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
    image_fingerprint,
    image_state_facts,
    json_digest,
    signal_fingerprint,
    signal_state_facts,
    state_facts,
)

from .helpers import ROI, image

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
    """Uncertainty rows are fingerprinted, ROI recorded, complex refused."""
    facts = signal_state_facts(_signal())
    assert facts["fingerprint"] == {
        "scheme": "datalab-signal-v1",
        "value": SIGNAL_FINGERPRINT,
    }
    assert facts["dtype"] == "float64" and facts["length"] == 4
    assert facts["rows"] == ["x", "y"] and facts["limits"] == []
    assert "roi" not in facts
    with_roi = signal_state_facts(_signal(roi=ROI([0.0, 0.5])))
    assert with_roi["limits"] == [] and with_roi["fingerprint"] == facts["fingerprint"]
    assert with_roi["roi"]["definition"] == {
        "single_rois": [{"coords": [0.0, 0.5], "type": "SegmentROI"}]
    }
    assert with_roi["roi"]["digest"] == json_digest(with_roi["roi"]["definition"])
    dy = np.full(4, 0.1)
    facts = signal_state_facts(_signal(dy=dy))
    assert facts["rows"] == ["x", "y", "dy"] and facts["limits"] == []
    assert facts["fingerprint"]["value"] == signal_fingerprint(
        _signal().x, _signal().y, dy=dy, xunit="s"
    )
    assert facts["fingerprint"]["value"] != SIGNAL_FINGERPRINT
    complex_y = np.array([1j, 0, 0, 0])
    assert signal_state_facts(_signal(y=complex_y))["limits"] == ["complex_dtype"]


def test_image_fingerprint_matches_independent_payload() -> None:
    """The image fingerprint equals a hand-built descriptor and payload."""
    data = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.uint16)
    descriptor = (
        b'{"coords":{"uniform":{"dx":0.5,"dy":2,"x0":-1,"y0":0}},"dtype":"<u2",'
        b'"scheme":"datalab-image-v1","shape":[2,3],'
        b'"units":{"x":"mm","y":"mm","z":"counts"}}'
    )
    payload = descriptor + b"\x00" + struct.pack("<6H", 1, 2, 3, 4, 5, 6)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    units = {"xunit": "mm", "yunit": "mm", "zunit": "counts"}
    got = image_fingerprint(data.astype(">u2"), x0=-1, dx=0.5, dy=2, **units)
    assert got == expected
    xc, yc = np.array([0.0, 1.0, 3.0]), np.array([0.0, 2.0])
    descriptor = (
        b'{"coords":{"nonuniform":{"x":3,"y":2}},"dtype":"<u2",'
        b'"scheme":"datalab-image-v1","shape":[2,3],'
        b'"units":{"x":"mm","y":"mm","z":"counts"}}'
    )
    payload = (
        descriptor
        + b"\x00"
        + struct.pack("<6H", 1, 2, 3, 4, 5, 6)
        + struct.pack("<3d", *xc)
        + struct.pack("<2d", *yc)
    )
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert image_fingerprint(data, xcoords=xc, ycoords=yc, **units) == expected
    with pytest.raises(ValueError):
        image_fingerprint(data, xcoords=yc, ycoords=xc)
    with pytest.raises(ValueError):
        image_fingerprint(np.zeros(3))


def test_image_state_facts() -> None:
    """Images get the image scheme, their shape and units; ROI recorded."""
    data = np.arange(6, dtype=np.float32).reshape(2, 3)
    facts = state_facts(image(data, roi=ROI([0, 0, 1, 1])))
    assert facts["kind"] == "image" and facts["shape"] == [2, 3]
    assert facts["dtype"] == "float32" and facts["rows"] == ["data"]
    assert facts["units"] == {"x": "mm", "y": "mm", "z": "counts"}
    assert facts["fingerprint"] == {
        "scheme": "datalab-image-v1",
        "value": image_fingerprint(data, xunit="mm", yunit="mm", zunit="counts"),
    }
    assert facts["roi"]["definition"]["single_rois"][0]["coords"] == [0, 0, 1, 1]
    coords = (np.array([0.0, 1.0, 3.0]), np.array([0.0, 2.0]))
    nonuniform = image_state_facts(image(data, coords=coords))
    assert nonuniform["fingerprint"]["value"] != facts["fingerprint"]["value"]
    complex_image = image_state_facts(image(data.astype(np.complex128)))
    assert complex_image["fingerprint"] is None
    assert complex_image["limits"] == ["complex_dtype"]
    assert state_facts(_signal())["kind"] == "signal"
