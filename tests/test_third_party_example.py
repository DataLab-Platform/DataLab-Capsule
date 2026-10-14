# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The third-party example reads the corpus with json, zipfile, hashlib and h5py.

It must agree with the library on every capsule of the corpus, without
importing it: this checks that the published profile is enough to implement a
reader.
"""

from __future__ import annotations

import ast
import importlib.util
import io
from pathlib import Path

import h5py
import numpy as np
import pytest

from datalab_capsule.integrity import image_fingerprint, signal_fingerprint_record
from datalab_capsule.verify import verify_capsule

from .corpus import CORPUS

EXAMPLE = Path(__file__).parents[1] / "examples" / "third_party_reader.py"
ALLOWED = {
    "__future__",
    "decimal",
    "hashlib",
    "h5py",
    "io",
    "json",
    "math",
    "numpy",
    "sys",
    "zipfile",
}


def _example():
    spec = importlib.util.spec_from_file_location("third_party_reader", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_imports_no_capsule_code() -> None:
    """The example only imports the standard library, h5py and NumPy."""
    tree = ast.parse(EXAMPLE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED, imported - ALLOWED


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in (CORPUS / "valid").glob("*.dlcapsule")),
)
def test_example_agrees_with_the_library(name: str) -> None:
    """Same manifest, workspace and state checks as ``verify_capsule``."""
    path = CORPUS / "valid" / name
    workspace = (
        CORPUS / "sources" / "difference_desktop.h5"
        if name == "referenced.dlcapsule"
        else None
    )
    found = _example().check_capsule(str(path), workspace and str(workspace))
    expected = verify_capsule(
        path, resolve_workspace=lambda _iri: workspace.read_bytes()
    )
    assert expected["valid"]
    assert found["manifest_ok"] and found["workspace"] == "ok"
    assert len(found["states"]) == expected["states_located"]
    assert all(found["states"].values())
    assert found["activities"]


def test_example_detects_defects() -> None:
    """A tampered manifest and an altered state are reported."""
    example = _example()
    tampered = example.check_capsule(
        str(CORPUS / "invalid" / "tampered-manifest.dlcapsule")
    )
    assert not tampered["manifest_ok"]
    altered = example.check_capsule(str(CORPUS / "invalid" / "altered-state.dlcapsule"))
    assert altered["manifest_ok"] and altered["workspace"] == "ok"
    assert list(altered["states"].values()).count(False) == 1
    assert example.main([str(CORPUS / "invalid" / "altered-state.dlcapsule")]) == 1


@pytest.mark.parametrize(
    "dtype", [np.float64, np.float32, np.complex128, np.complex64, np.float16]
)
def test_example_signal_fingerprints_match_the_library(dtype) -> None:
    """Signals whose rows differ in dtype (``datalab-signal-v2``) are read alike."""
    x = np.array([0.0, 0.5, 1.0, 1.5])
    y = np.array([1.0, -2.0, 3.5, 0.25]) * (
        1 - 1j if np.dtype(dtype).kind == "c" else 1
    )
    xydata = np.vstack([x, y, np.full(4, np.nan), np.full(4, 0.125)]).astype(dtype)
    with h5py.File(io.BytesIO(), "w") as h5file:
        node = h5file.create_group("s")
        node.attrs["xunit"], node.attrs["yunit"] = "s", "V"
        node["xydata"] = xydata
        found = _example().signal_fingerprint(node)
    expected = signal_fingerprint_record(*xydata, "s", "V")
    assert found == expected["value"]
    v1 = np.dtype(dtype) == np.float64
    assert expected["scheme"] == ("datalab-signal-v1" if v1 else "datalab-signal-v2")


def test_example_complex_image_fingerprint_matches_the_library() -> None:
    """Complex images keep the ``datalab-image-v1`` scheme."""
    data = (np.arange(6).reshape(2, 3) * (1 + 0.5j)).astype(np.complex64)
    with h5py.File(io.BytesIO(), "w") as h5file:
        node = h5file.create_group("i")
        node.attrs.update({"xunit": "mm", "yunit": "mm", "zunit": ""})
        node["data"] = data
        found = _example().image_fingerprint(node)
    assert found == image_fingerprint(data, xunit="mm", yunit="mm")
