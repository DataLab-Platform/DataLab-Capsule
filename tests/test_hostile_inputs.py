# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Hostile inputs: deep nesting and malformed embedded JSON are refused cleanly."""

from __future__ import annotations

import copy
import io
import json
import zipfile

import h5py
import pytest

from datalab_capsule.archive import CapsuleError, read_capsule
from datalab_capsule.cli import main
from datalab_capsule.hdf5 import ProvenanceFormatError, read_roi
from datalab_capsule.integrity import MAX_JSON_DEPTH, check_json_depth, load_json
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    ManifestError,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.verify import verify_capsule

from .test_diff_verify import _chain, _manifest


def _manifest_zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, text)
    return buffer.getvalue()


def test_json_depth_limit() -> None:
    """Nesting up to the limit is accepted, deeper is refused, scalars pass."""
    nested: object = 0
    for _index in range(MAX_JSON_DEPTH):
        nested = [nested]
    check_json_depth(nested)
    with pytest.raises(ValueError, match="nested"):
        check_json_depth([nested])
    check_json_depth("text")
    with pytest.raises(ValueError, match="nested"):
        load_json("[" * 100_000 + "]" * 100_000)


def test_deeply_nested_manifest_is_invalid(tmp_path, capsys) -> None:
    """A nested manifest is an invalid capsule, not a crash."""
    data = _manifest_zip("[" * 100_000 + "]" * 100_000)
    with pytest.raises(ManifestError, match="nested"):
        read_capsule(data)
    path = tmp_path / "nested.dlcapsule"
    path.write_bytes(data)
    assert main(["validate", str(path)]) == 1
    assert "Invalid capsule" in capsys.readouterr().err
    assert main(["verify", str(path)]) == 1


def _refingerprinted(manifest: dict) -> dict:
    root = next(e for e in manifest["@graph"] if e["@id"] == "./")
    root.pop("dlc:manifestFingerprint")
    root["dlc:manifestFingerprint"] = manifest_fingerprint(manifest)
    return manifest


def test_embedded_json_is_validated() -> None:
    """Parameters and other embedded JSON must be valid, bounded JSON text."""
    _data, ledger, locators = _chain()
    manifest = _manifest(ledger, locators)
    validate_manifest(manifest)
    parameter = next(e for e in manifest["@graph"] if "dlc:jsonValue" in e)
    for bad in ("not json", "[" * 200 + "]" * 200, {"value": 1}):
        hostile = copy.deepcopy(manifest)
        entity = next(e for e in hostile["@graph"] if e["@id"] == parameter["@id"])
        entity["dlc:jsonValue"] = bad
        with pytest.raises(ManifestError, match="invalid dlc:jsonValue"):
            validate_manifest(_refingerprinted(hostile))


def test_unreadable_structure_is_invalid() -> None:
    """A valid-looking manifest that cannot be summarised is invalid."""
    _data, ledger, locators = _chain()
    manifest = _manifest(ledger, locators)
    hostile = copy.deepcopy(manifest)
    parameter = next(e for e in hostile["@graph"] if "dlc:jsonValue" in e)
    del parameter["name"]
    with pytest.raises(ManifestError, match="cannot be summarised"):
        validate_manifest(_refingerprinted(hostile))
    assert json.dumps(manifest)  # the original stays valid
    validate_manifest(manifest)


def test_mistyped_references_and_root_fields_are_invalid() -> None:
    """A non-text @id or root field is an invalid manifest, not a crash."""
    _data, ledger, locators = _chain()
    manifest = _manifest(ledger, locators)
    hostile = copy.deepcopy(manifest)
    root = next(e for e in hostile["@graph"] if e["@id"] == "./")
    root["conformsTo"] = [{"@id": 123}]
    with pytest.raises(ManifestError, match="string @id"):
        validate_manifest(_refingerprinted(hostile))
    for key, value in (("name", ["a", "b"]), ("license", {"@id": "x"})):
        hostile = copy.deepcopy(manifest)
        root = next(e for e in hostile["@graph"] if e["@id"] == "./")
        root[key] = value
        with pytest.raises(ManifestError, match=f"{key} of the root data entity"):
            validate_manifest(_refingerprinted(hostile))


def test_nested_hdf5_metadata_is_refused() -> None:
    """An ROI nested deeper than the limit is refused, not decoded recursively."""
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as h5file:
        node = h5file.create_group("/DataLab_Sig/g/s").create_group("metadata")
        node = node.create_group("_roi_")
        for _index in range(MAX_JSON_DEPTH + 1):
            node.attrs["__seq"] = 1
            node = node.create_group("__seq0")
        node.attrs["__dict"] = 1
        with pytest.raises(ProvenanceFormatError, match="nested too deeply"):
            read_roi(h5file, "/DataLab_Sig/g/s")


def test_damaged_entry_is_an_invalid_capsule() -> None:
    """A CRC mismatch while reading an entry is refused as an invalid capsule."""
    data = bytearray(_manifest_zip('{"a": 1}' * 8))
    position = data.find(b'{"a"')
    data[position + 1] ^= 0xFF
    with pytest.raises(CapsuleError, match="Damaged entry"):
        read_capsule(bytes(data))
    result = verify_capsule(bytes(data))
    assert not result["valid"]
    assert "Damaged entry" in result["problems"][0]
