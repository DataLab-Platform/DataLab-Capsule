# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for capsule manifests, archives and the command line."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from datalab_capsule import archive as archive_module
from datalab_capsule.archive import CapsuleError, create_from_hdf5, read_capsule
from datalab_capsule.cli import main
from datalab_capsule.hdf5 import save_ledger
from datalab_capsule.integrity import signal_state_facts
from datalab_capsule.ledger import Ledger
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    WORKSPACE_NAME,
    ManifestError,
    build_manifest,
    inspect_manifest,
    load_ro_crate_context,
    manifest_fingerprint,
    validate_manifest,
)

from .helpers import signal

FIXTURES = Path(__file__).parent / "fixtures" / "expressiveness"
WORKSPACES = Path(__file__).parent / "fixtures" / "workspaces"
LEDGER_FIXTURES = sorted(
    p for p in FIXTURES.glob("*.json") if "ledger" in json.loads(p.read_text("utf-8"))
)
SHA = "sha256:" + "0" * 64
DATE = "2026-10-03T08:00:00.000Z"
# Fingerprint of the manifest of ``one_to_n.json`` built with SHA, 1234 and DATE.
GOLDEN_FINGERPRINT = (
    "sha256:a505f33f7d37c00a5280520995718d26d6ab9ef3b98b71723bbfb4ff1a13f7e2"
)


def fixture_manifest(path: Path) -> tuple[dict, dict]:
    """Return a fixture and its manifest (fixed digest, size and date)."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    ledger = Ledger.from_dict(fixture["ledger"])
    manifest = build_manifest(
        ledger,
        fixture["locators"],
        workspace_sha256=SHA,
        workspace_size=1234,
        date_published=DATE,
    )
    return fixture, manifest


def _target(ref: str) -> str:
    return ref.removeprefix("#state-")


@pytest.mark.parametrize("path", LEDGER_FIXTURES, ids=lambda p: p.stem)
def test_fixture_manifests(path: Path) -> None:
    """Every expressiveness fixture gives a valid manifest, order included."""
    fixture, manifest = fixture_manifest(path)
    validate_manifest(manifest)
    summary = inspect_manifest(manifest)
    activities = fixture["ledger"]["activities"]
    assert len(summary["activities"]) == len(activities)
    for act, original in zip(summary["activities"], activities):
        call = original["call"]
        assert act["replayable"] is (call["operation"] is not None)
        assert [(i["role"], _target(i["target"])) for i in act["inputs"]] == [
            (i["role"], i["binding"]["state_id"]) for i in call["inputs"]
        ]
        assert [o["role"] for o in act["outputs"]] == [
            o["role"] for o in original["outputs"]
        ]
        assert act["parameters"] == (call["parameters"] or {})
        if call["parameters"] is None:
            assert "parameters_not_encoded" in act["limits"]
    located = {
        s["id"].removeprefix("#state-") for s in summary["states"] if s["locator"]
    }
    assert located == set(fixture["locators"])


def test_fan_out_and_opaque_steps() -> None:
    """Inspection shows the fan-out and the opaque instrument."""
    _fixture, manifest = fixture_manifest(
        FIXTURES / "fan_out_missing_intermediate.json"
    )
    assert inspect_manifest(manifest)["fan_out"]
    _fixture, manifest = fixture_manifest(FIXTURES / "opaque_not_encoded.json")
    (act,) = inspect_manifest(manifest)["activities"]
    assert act["replayable"] is False
    entities = {e["@id"]: e for e in manifest["@graph"]}
    action = entities[act["id"]]
    assert all(
        "dlc:operationId" not in entities[ref["@id"]] for ref in action["instrument"]
    )


def test_manifest_fingerprint() -> None:
    """The fingerprint excludes itself, detects changes and is stable."""
    _fixture, manifest = fixture_manifest(FIXTURES / "one_to_n.json")
    root = next(e for e in manifest["@graph"] if e["@id"] == "./")
    assert root["dlc:manifestFingerprint"] == manifest_fingerprint(manifest)
    assert root["dlc:manifestFingerprint"] == GOLDEN_FINGERPRINT
    root["name"] = "changed"
    with pytest.raises(ManifestError, match="fingerprint"):
        validate_manifest(manifest)


def test_invalid_manifests() -> None:
    """Missing root properties, dangling references and unknown terms fail."""
    _fixture, manifest = fixture_manifest(FIXTURES / "difference.json")
    entities = {e["@id"]: e for e in manifest["@graph"]}
    del entities["./"]["license"]
    entities["./"]["mentions"].append({"@id": "#activity-missing"})
    entities[MANIFEST_NAME]["unknownTerm"] = 1
    with pytest.raises(ManifestError) as info:
        validate_manifest(manifest)
    message = str(info.value)
    for expected in ("license", "#activity-missing", "unknownTerm"):
        assert expected in message


def test_embedded_context_is_cc0() -> None:
    """The embedded RO-Crate context is the CC0 1.3 context."""
    context = load_ro_crate_context()
    assert context["version"] == "1.3.0"
    assert context["license"] == {
        "@id": "https://creativecommons.org/publicdomain/zero/1.0/"
    }


def workspace_bytes() -> bytes:
    """Return a small workspace holding a signal and a provenance block."""
    sig = signal([-2.0, 0.0, 1.0, 4.0])
    ledger = Ledger()
    ledger.observe("00000000-0000-4000-8000-000000000001", signal_state_facts(sig))
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as h5file:
        group = h5file.create_group("/DataLab_Sig/g: G/s: S")
        group.attrs["xunit"] = "s"
        group.attrs["yunit"] = ""
        group["xydata"] = np.vstack([sig.x, sig.y])
        group.create_group("metadata").attrs["__uuid"] = (
            "00000000-0000-4000-8000-000000000001"
        )
        h5file["DataLab_Version"] = "test"
        save_ledger(h5file, ledger)
    return buffer.getvalue()


def test_create_and_read_capsule(tmp_path: Path) -> None:
    """A capsule stores both entries uncompressed and reads back intact."""
    workspace = workspace_bytes()
    data = create_from_hdf5(workspace, name="Test", date_published=DATE)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
    assert [i.filename for i in infos] == [MANIFEST_NAME, WORKSPACE_NAME]
    assert all(i.compress_type == zipfile.ZIP_STORED for i in infos)
    capsule = read_capsule(data)
    assert capsule.workspace == workspace
    (state,) = inspect_manifest(capsule.manifest)["states"]
    assert state["locator"] == "/DataLab_Sig/g: G/s: S"
    path = tmp_path / "test.dlcapsule"
    path.write_bytes(data)
    assert read_capsule(path).workspace == workspace
    with pytest.raises(CapsuleError, match="no provenance block"):
        buffer = io.BytesIO()
        with h5py.File(buffer, "w") as h5file:
            h5file["DataLab_Version"] = "test"
        create_from_hdf5(buffer.getvalue())


def _zip(entries, compression=zipfile.ZIP_STORED, mode=0o644) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            info.external_attr = mode << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def _encrypted(data: bytes) -> bytes:
    """Set the 'encrypted' flag of every central directory header."""
    raw = bytearray(data)
    start = raw.find(b"PK\x01\x02")
    while start >= 0:
        raw[start + 8] |= 0x1
        start = raw.find(b"PK\x01\x02", start + 4)
    return bytes(raw)


def _entries() -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(create_from_hdf5(workspace_bytes()))) as archive:
        return [(i.filename, archive.read(i)) for i in archive.infolist()]


@pytest.mark.parametrize(
    "name", ["../evil", "/abs", "C:evil", "dir\\evil", "extra.txt"]
)
def test_unsafe_names_are_refused(name: str) -> None:
    """Traversal, absolute, drive, backslash and unexpected names fail."""
    with pytest.raises(CapsuleError):
        read_capsule(_zip([*_entries(), (name, b"x")]))


def test_unsafe_archives_are_refused(monkeypatch) -> None:
    """Duplicates, links, encryption, oversize, ratio and tampering fail."""
    entries = _entries()
    with pytest.warns(UserWarning):
        duplicate = _zip([*entries, entries[0]])
    with pytest.raises(CapsuleError, match="Duplicate"):
        read_capsule(duplicate)
    with pytest.raises(CapsuleError, match="Link"):
        read_capsule(_zip(entries, mode=stat.S_IFLNK | 0o777))
    with pytest.raises(CapsuleError, match="Encrypted"):
        read_capsule(_encrypted(_zip(entries)))
    with pytest.raises(CapsuleError, match="Missing"):
        read_capsule(_zip(entries[:1]))
    monkeypatch.setitem(archive_module._LIMITS, WORKSPACE_NAME, 100)
    with pytest.raises(CapsuleError, match="too large"):
        read_capsule(_zip(entries))
    monkeypatch.undo()
    padded = [entries[0], (WORKSPACE_NAME, entries[1][1] + b"\0" * 10**6)]
    with pytest.raises(CapsuleError, match="ratio"):
        read_capsule(_zip(padded, compression=zipfile.ZIP_DEFLATED))
    tampered = [entries[0], (WORKSPACE_NAME, entries[1][1][:-1] + b"\1")]
    with pytest.raises(CapsuleError, match="does not match"):
        read_capsule(_zip(tampered))
    with pytest.raises(CapsuleError, match="Not a ZIP"):
        read_capsule(b"not a zip")


def test_command_line(tmp_path: Path, capsys) -> None:
    """``validate`` and ``inspect`` report on valid and invalid capsules."""
    path = tmp_path / "test.dlcapsule"
    path.write_bytes(create_from_hdf5(workspace_bytes(), date_published=DATE))
    assert main(["validate", str(path)]) == 0
    assert "Valid capsule" in capsys.readouterr().out
    assert main(["inspect", str(path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["date_published"] == DATE
    assert main(["inspect", str(path)]) == 0
    assert "States (1)" in capsys.readouterr().out
    bad = tmp_path / "bad.dlcapsule"
    bad.write_bytes(b"not a zip")
    assert main(["validate", str(bad)]) == 1
    assert "Invalid capsule" in capsys.readouterr().err


@pytest.mark.parametrize("edition", ["desktop", "web"])
def test_chain_capsules(edition: str) -> None:
    """Capsules of both editions' chain workspaces describe the whole chain."""
    workspace = (WORKSPACES / f"{edition}_chain.h5").read_bytes()
    capsule = read_capsule(create_from_hdf5(workspace))
    assert capsule.workspace == workspace
    summary = inspect_manifest(capsule.manifest)
    a1, a2, a3, a4 = summary["activities"]
    assert [a["replayable"] for a in summary["activities"]] == [
        False,
        True,
        True,
        True,
    ]
    assert a1["parameters"] == {"value": 1.0}
    assert a1["name"].endswith("addition_constant")
    assert [a["parameters"]["method"] for a in (a2, a3, a4)] == [
        "maximum",
        "amplitude",
        "amplitude",
    ]
    s0 = a1["inputs"][0]["target"]
    assert summary["fan_out"] == [s0]
    assert a4["inputs"][0]["target"] == s0
    assert a2["inputs"][0]["target"] == a1["outputs"][0]["target"]
    assert a3["inputs"][0]["target"] == a2["outputs"][0]["target"]
    assert all(state["locator"] for state in summary["states"])
    entities = {e["@id"]: e for e in capsule.manifest["@graph"]}
    editions = {
        entities[ref["@id"]]["name"]
        for act in summary["activities"]
        for ref in entities[act["id"]]["instrument"]
        if ref["@id"].startswith("#edition-")
    }
    assert editions == {"DataLab" if edition == "desktop" else "DataLab-Web"}
