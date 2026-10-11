# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The third-party example reads the corpus with json, zipfile, hashlib and h5py.

It must agree with the library on every capsule of the corpus, without
importing it: this checks that the published profile is enough to implement a
reader.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

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
