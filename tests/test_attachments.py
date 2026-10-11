# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Light attachments: declared in the manifest, checked, returned as data."""

from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path

import pytest

from datalab_capsule.archive import (
    MAX_ATTACHMENTS,
    CapsuleError,
    create_from_hdf5,
    read_capsule,
)
from datalab_capsule.cli import main
from datalab_capsule.diff import diff_manifests
from datalab_capsule.manifest import (
    ATTACHMENT_KEY,
    ManifestError,
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)

from .test_capsule import DATE, workspace_bytes

NOTE = {
    "name": "protocol.txt",
    "data": b"Acquisition protocol, version 2.\n",
    "media_type": "text/plain",
    "description": "How the signal was acquired",
}
SCRIPT = {
    "name": "macro.py",
    "data": b"print('never run')\n",
    "media_type": "text/x-python",
}


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_attachments_are_declared_and_returned_as_data() -> None:
    """Each attachment is a declared File with size and SHA-256, returned as bytes."""
    data = create_from_hdf5(
        workspace_bytes(), date_published=DATE, attachments=[NOTE, SCRIPT]
    )
    assert sorted(_entries(data)) == [
        "attachments/macro.py",
        "attachments/protocol.txt",
        "ro-crate-metadata.json",
        "workspace.h5",
    ]
    capsule = read_capsule(data)
    assert capsule.attachments == {
        "protocol.txt": NOTE["data"],
        "macro.py": SCRIPT["data"],
    }
    summary = inspect_manifest(capsule.manifest)
    assert [a["name"] for a in summary["attachments"]] == ["protocol.txt", "macro.py"]
    first = summary["attachments"][0]
    assert first["size"] == str(len(NOTE["data"]))
    assert first["media_type"] == "text/plain"
    assert first["description"] == "How the signal was acquired"
    assert summary["workspace"]["id"] == "workspace.h5"


def test_no_attachment_keeps_the_manifest_unchanged() -> None:
    """Without attachments, the manifest and its fingerprint are as before."""
    workspace = workspace_bytes()
    plain = read_capsule(create_from_hdf5(workspace, date_published=DATE))
    empty = read_capsule(
        create_from_hdf5(workspace, date_published=DATE, attachments=[])
    )
    assert manifest_fingerprint(plain.manifest) == manifest_fingerprint(empty.manifest)
    assert plain.attachments == {}
    assert inspect_manifest(plain.manifest)["attachments"] == []
    with_note = read_capsule(
        create_from_hdf5(workspace, date_published=DATE, attachments=[NOTE])
    )
    assert manifest_fingerprint(with_note.manifest) != manifest_fingerprint(
        plain.manifest
    )


def test_referenced_workspace_with_attachment() -> None:
    """A referenced workspace and attachments can be combined."""
    data = create_from_hdf5(
        workspace_bytes(),
        date_published=DATE,
        workspace_ref="https://data.example.org/w.h5",
        attachments=[NOTE],
    )
    capsule = read_capsule(data)
    assert capsule.workspace is None
    assert capsule.attachments == {"protocol.txt": NOTE["data"]}


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", ".hidden", "a/b.txt", "", "x" * 101, "space name.txt"],
)
def test_unsafe_names_are_refused(name: str) -> None:
    """Attachment names are plain, safe file names."""
    with pytest.raises(ManifestError, match="Invalid attachment name"):
        create_from_hdf5(workspace_bytes(), attachments=[dict(NOTE, name=name)])


def test_limits_on_creation() -> None:
    """Too many or too large attachments are refused when creating."""
    many = [dict(NOTE, name=f"n{i}.txt") for i in range(MAX_ATTACHMENTS + 1)]
    with pytest.raises(CapsuleError, match="Too many attachments"):
        create_from_hdf5(workspace_bytes(), attachments=many)
    with pytest.raises(ManifestError, match="unique"):
        create_from_hdf5(workspace_bytes(), attachments=[NOTE, NOTE])
    with pytest.raises(ManifestError, match="media type"):
        create_from_hdf5(workspace_bytes(), attachments=[dict(NOTE, media_type="text")])


def test_reader_refuses_altered_missing_or_undeclared_attachments() -> None:
    """Only declared attachments matching size and SHA-256 are accepted."""
    entries = _entries(
        create_from_hdf5(workspace_bytes(), date_published=DATE, attachments=[NOTE])
    )
    altered = dict(entries, **{"attachments/protocol.txt": b"Edited.\n"})
    with pytest.raises(CapsuleError, match="does not match"):
        read_capsule(_zip(altered))
    missing = {k: v for k, v in entries.items() if not k.startswith("attachments/")}
    with pytest.raises(CapsuleError, match="Missing attachments"):
        read_capsule(_zip(missing))
    extra = dict(entries, **{"attachments/extra.txt": b"x"})
    with pytest.raises(CapsuleError, match="Undeclared attachments"):
        read_capsule(_zip(extra))
    unsafe = dict(entries, **{"attachments/.hidden": b"x"})
    with pytest.raises(CapsuleError, match="Unsafe entry name"):
        read_capsule(_zip(unsafe))
    nested = dict(entries, **{"attachments/a/b.txt": b"x"})
    with pytest.raises(CapsuleError, match="Unsafe entry name"):
        read_capsule(_zip(nested))


def test_manifest_validation_of_attachments() -> None:
    """A declared attachment must be a File marked true, with valid fields."""
    manifest = read_capsule(
        create_from_hdf5(workspace_bytes(), date_published=DATE, attachments=[NOTE])
    ).manifest
    target = "attachments/protocol.txt"
    for field, value in (("sha256", "zz"), ("contentSize", "-3"), (ATTACHMENT_KEY, 1)):
        hostile = copy.deepcopy(manifest)
        entity = next(e for e in hostile["@graph"] if e["@id"] == target)
        entity[field] = value
        root = next(e for e in hostile["@graph"] if e["@id"] == "./")
        root.pop("dlc:manifestFingerprint")
        root["dlc:manifestFingerprint"] = manifest_fingerprint(hostile)
        with pytest.raises(ManifestError, match="attachments/protocol.txt"):
            validate_manifest(hostile)


def test_cli_lists_and_writes_attachments(tmp_path: Path, capsys) -> None:
    """The command line lists attachments and writes one only where asked."""
    capsule = tmp_path / "c.dlcapsule"
    capsule.write_bytes(
        create_from_hdf5(workspace_bytes(), date_published=DATE, attachments=[NOTE])
    )
    assert main(["inspect", str(capsule)]) == 0
    assert "Attachment protocol.txt (text/plain" in capsys.readouterr().out
    output = tmp_path / "out.txt"
    assert main(["attachment", str(capsule), "protocol.txt", str(output)]) == 0
    assert output.read_bytes() == NOTE["data"]
    assert main(["attachment", str(capsule), "protocol.txt", str(output)]) == 1
    assert "Cannot write" in capsys.readouterr().err
    assert main(["attachment", str(capsule), "other.txt", str(tmp_path / "x")]) == 1
    assert not (tmp_path / "x").exists()


def test_diff_reports_attachment_changes_in_their_own_group() -> None:
    """Attachment changes are listed apart; their content is not interpreted."""
    data = workspace_bytes()
    old = read_capsule(create_from_hdf5(data, date_published=DATE)).manifest
    new = read_capsule(
        create_from_hdf5(data, date_published=DATE, attachments=[NOTE])
    ).manifest
    result = diff_manifests(old, new)
    assert not result["identical"]
    assert result["attachments"] == [
        {"change": "added", "item": "attachment", "id": "protocol.txt"}
    ]
    assert not (result["scientific"] or result["presentation"])
    edited = read_capsule(
        create_from_hdf5(
            data, date_published=DATE, attachments=[{**NOTE, "data": b"v2\n"}]
        )
    ).manifest
    changed = diff_manifests(new, edited)
    assert {c["field"] for c in changed["attachments"]} == {"sha256", "size"}
    assert not (changed["scientific"] or changed["presentation"])
