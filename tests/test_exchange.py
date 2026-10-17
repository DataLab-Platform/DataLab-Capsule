# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Capsule exchange functions shared by the editions' capsule interfaces."""

from __future__ import annotations

import io
import json
from pathlib import Path

import h5py
import pytest

from datalab_capsule.archive import CapsuleError, create_from_hdf5, read_capsule
from datalab_capsule.compare import build_report, compare_environments
from datalab_capsule.exchange import (
    attachment_name,
    capsule_attachment,
    capsule_origin,
    check_registry_revision,
    diff_checked_manifests,
    inspect_capsule,
    open_capsule,
    review_of,
)
from datalab_capsule.hdf5 import read_block
from datalab_capsule.manifest import ManifestError, manifest_fingerprint
from datalab_capsule.review import ReviewError, validate_review

from .test_capsule import DATE

CHAIN = Path(__file__).parent / "fixtures" / "workspaces" / "desktop_chain.h5"
REF = "https://data.example.org/workspace.h5"
NOTE = {
    "name": "protocol.txt",
    "data": b"Bench 2.\n",
    "media_type": "text/plain",
    "description": "Protocol",
}


def _chain() -> bytes:
    return CHAIN.read_bytes()


def _report(activity_id: str) -> dict:
    return build_report(
        activity_id=activity_id,
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference=None,
        environment=compare_environments(None, None, None, None),
    )


def test_attachment_name() -> None:
    """Proposed names follow the capsule profile and are unique."""
    assert attachment_name("protocol.txt") == "protocol.txt"
    assert attachment_name("my notes (v2).txt") == "my_notes_v2_.txt"
    assert attachment_name(".hidden") == "file.hidden"
    assert attachment_name("é.txt") == "file_.txt"
    assert attachment_name("a.txt", {"a.txt", "2-a.txt"}) == "3-a.txt"
    assert len(attachment_name("x" * 300)) == 96


def test_open_and_origin() -> None:
    """Opening returns the workspace and the origin of a later revision."""
    data = _chain()
    capsule = create_from_hdf5(data, name="chain", attachments=[NOTE])
    workspace, origin = open_capsule(capsule)
    assert workspace == data
    manifest = read_capsule(capsule).manifest
    assert origin == capsule_origin(manifest)
    assert origin == {
        "fingerprint": manifest_fingerprint(manifest),
        "name": "chain",
        "attachments": ["protocol.txt"],
    }
    assert capsule_attachment(capsule, "protocol.txt") == NOTE["data"]
    with pytest.raises(CapsuleError, match="No such attachment"):
        capsule_attachment(capsule, "other.txt")


def test_open_referenced_workspace() -> None:
    """A referenced workspace must be supplied and match the manifest."""
    data = _chain()
    capsule = create_from_hdf5(data, workspace_ref=REF)
    with pytest.raises(CapsuleError, match="select the workspace file"):
        open_capsule(capsule)
    assert open_capsule(capsule, data)[0] == data
    with pytest.raises(CapsuleError):
        open_capsule(capsule, data + b"x")


def test_inspect_keeps_integrity_and_documents_apart() -> None:
    """Rejected documents are not integrity problems."""
    data = _chain()
    with h5py.File(io.BytesIO(data), "r") as h5file:
        ledger, _locators = read_block(h5file)
    capsule = create_from_hdf5(data, date_published=DATE)
    manifest = read_capsule(capsule).manifest
    good = _report(ledger.activities[0]["activity_id"])
    stranger = _report("00000000-0000-4000-8000-0000000000ff")
    review = review_of(manifest, reviewer="A", statement="Checked.")
    result = inspect_capsule(
        capsule,
        reports=[json.dumps(good), json.dumps(stranger), "not json"],
        reviews=[json.dumps(review)],
    )
    assert result["manifest"] == manifest
    assert result["summary"]["fingerprint"] == manifest_fingerprint(manifest)
    integrity = result["integrity"]
    assert integrity["valid"] and integrity["problems"] == []
    assert integrity["workspace"] == "verified"
    documents = result["documents"]
    assert [r["status"] for r in documents["reports"]] == [
        "valid",
        "The report names an activity absent from the capsule",
    ]
    assert documents["reviews"][0]["status"] == "valid"
    assert [(u["kind"], u["index"]) for u in documents["unreadable"]] == [("report", 2)]


def test_inspect_invalid_and_referenced() -> None:
    """An unreadable capsule gives no summary; a referenced one is checked once
    its workspace is supplied."""
    broken = inspect_capsule(b"not a capsule")
    assert broken["summary"] is None and broken["documents"] is None
    assert not broken["integrity"]["valid"]
    assert broken["integrity"]["problems"][0].startswith("Invalid capsule")
    data = _chain()
    capsule = create_from_hdf5(data, workspace_ref=REF)
    assert inspect_capsule(capsule)["integrity"]["workspace"] == "not_available"
    supplied = inspect_capsule(capsule, workspace=data)["integrity"]
    assert supplied["workspace"] == "verified" and supplied["valid"]


def test_registry_revision_is_checked() -> None:
    """The manifest must be valid and the one requested; reviews are checked."""
    capsule = create_from_hdf5(_chain(), date_published=DATE)
    manifest = read_capsule(capsule).manifest
    fingerprint = manifest_fingerprint(manifest)
    review = review_of(manifest, reviewer="B", statement="Replayed.")
    other = review_of(manifest, reviewer="C", statement="Other.")
    other["statement"] = "Altered."
    checked = check_registry_revision(
        json.dumps(manifest), fingerprint, [json.dumps(review), other, "{"]
    )
    assert checked["manifest"] == manifest
    assert checked["summary"]["fingerprint"] == fingerprint
    assert [r["status"] == "valid" for r in checked["reviews"]] == [
        True,
        False,
        False,
    ]
    with pytest.raises(CapsuleError, match="another revision"):
        check_registry_revision(json.dumps(manifest), "sha256:" + "0" * 64)


def test_diff_and_review_validate_manifests() -> None:
    """Diffs and reviews refuse invalid manifests."""
    data = _chain()
    parent = read_capsule(create_from_hdf5(data, date_published=DATE)).manifest
    child = read_capsule(
        create_from_hdf5(
            data,
            date_published=DATE,
            revision_of=manifest_fingerprint(parent),
            attachments=[NOTE],
        )
    ).manifest
    diff = diff_checked_manifests(parent, child)
    assert diff["revision_of_old"] and not diff["identical"]
    assert len(diff["attachments"]) == 1
    with pytest.raises(ManifestError):
        diff_checked_manifests(parent, {"@graph": []})
    review = review_of(child, reviewer="A", statement="Fine.", verdict="endorsed")
    validate_review(review, child)
    with pytest.raises(ManifestError):
        review_of({"@graph": []}, reviewer="A", statement="Fine.")
    with pytest.raises(ReviewError):
        review_of(child, reviewer="A", statement="Fine.", verdict="maybe")
