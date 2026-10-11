# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for revisions (one parent), review assertions and referenced workspaces."""

from __future__ import annotations

import copy
import io
import json
import zipfile

import pytest

from datalab_capsule.archive import CapsuleError, create_from_hdf5, read_capsule
from datalab_capsule.ledger import Ledger
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    REVISION_KEY,
    ManifestError,
    build_manifest,
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.review import ReviewError, create_review, validate_review

from .test_capsule import DATE, SHA, workspace_bytes


def _manifest(revision_of=None) -> dict:
    return build_manifest(
        Ledger(),
        {},
        workspace_sha256=SHA,
        workspace_size=1,
        date_published=DATE,
        revision_of=revision_of,
    )


def test_revision_names_one_parent() -> None:
    """A revision names its parent's manifest fingerprint, part of its own."""
    parent = _manifest()
    parent_fp = manifest_fingerprint(parent)
    child = _manifest(revision_of=parent_fp)
    validate_manifest(child)
    root = next(e for e in child["@graph"] if e["@id"] == "./")
    assert root[REVISION_KEY] == parent_fp
    assert inspect_manifest(child)["revision_of"] == parent_fp
    assert inspect_manifest(parent)["revision_of"] is None
    assert manifest_fingerprint(child) != parent_fp
    with pytest.raises(ManifestError):
        _manifest(revision_of="not a fingerprint")
    with pytest.raises(ManifestError):
        _manifest(revision_of=["sha256:" + "1" * 64, "sha256:" + "2" * 64])
    tampered = copy.deepcopy(child)
    next(e for e in tampered["@graph"] if e["@id"] == "./")[REVISION_KEY] = (
        "sha256:" + "f" * 64
    )
    with pytest.raises(ManifestError, match="fingerprint does not match"):
        validate_manifest(tampered)


def test_capsule_records_its_parent() -> None:
    """A capsule created as a revision keeps the parent in its manifest."""
    parent = read_capsule(create_from_hdf5(workspace_bytes(), date_published=DATE))
    parent_fp = manifest_fingerprint(parent.manifest)
    child = read_capsule(
        create_from_hdf5(workspace_bytes(), date_published=DATE, revision_of=parent_fp)
    )
    assert inspect_manifest(child.manifest)["revision_of"] == parent_fp


def test_review_targets_one_revision() -> None:
    """A review is a separate, fingerprinted document targeting one revision."""
    manifest = _manifest()
    snapshot = copy.deepcopy(manifest)
    review = create_review(
        manifest,
        reviewer="A. Reviewer",
        reviewer_id="https://orcid.org/0000-0000-0000-0000",
        statement="Replayed the qualified steps; results identical.",
        verdict="endorsed",
        reports=[{"activity_id": "a1", "verdict": "exact", "extra": "dropped"}],
        date=DATE,
    )
    validate_review(review, manifest)
    assert review["target"] == manifest_fingerprint(manifest)
    assert review["reports"] == [{"activity_id": "a1", "verdict": "exact"}]
    # The capsule is unchanged by the review.
    assert manifest == snapshot
    other = _manifest(revision_of=manifest_fingerprint(manifest))
    with pytest.raises(ReviewError, match="another capsule revision"):
        validate_review(review, other)
    altered = dict(review, statement="Changed afterwards.")
    with pytest.raises(ReviewError, match="fingerprint"):
        validate_review(altered)
    with pytest.raises(ReviewError):
        create_review(manifest, reviewer="R", statement="s", verdict="approved")
    bare = create_review(
        manifest_fingerprint(manifest), reviewer="R", statement="s", date=DATE
    )
    assert bare["verdict"] is None and bare["reports"] == []
    validate_review(bare, manifest)


def test_review_of_a_tampered_manifest_fails() -> None:
    """A review never validates against a manifest changed after it was written."""
    data = create_from_hdf5(workspace_bytes(), date_published=DATE)
    manifest = read_capsule(data).manifest
    review = create_review(manifest, reviewer="R", statement="s", date=DATE)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        text = archive.read(MANIFEST_NAME).decode("utf-8")
    changed = text.replace("DataLab workspace", "Renamed workspace", 1)
    with pytest.raises(ReviewError):
        validate_review(review, json.loads(changed))


REF = "https://example.org/data/workspace.h5"


def test_referenced_workspace_is_resolved_explicitly() -> None:
    """A referenced workspace is obtained only through the caller's resolver."""
    workspace = workspace_bytes()
    data = create_from_hdf5(workspace, date_published=DATE, workspace_ref=REF)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == [MANIFEST_NAME]
    unresolved = read_capsule(data)
    assert unresolved.workspace is None and unresolved.workspace_ref == REF
    summary = inspect_manifest(unresolved.manifest)
    assert summary["workspace"]["id"] == REF and summary["workspace"]["referenced"]
    asked = []

    def resolver(iri: str) -> bytes:
        asked.append(iri)
        return workspace

    resolved = read_capsule(data, resolve_workspace=resolver)
    assert asked == [REF] and resolved.workspace == workspace
    with pytest.raises(CapsuleError, match="does not match"):
        read_capsule(data, resolve_workspace=lambda _iri: workspace + b"x")
    # Same scientific content as the included form; only the workspace entity
    # and the references to it differ.
    included = read_capsule(create_from_hdf5(workspace, date_published=DATE))
    assert inspect_manifest(included.manifest)["workspace"]["referenced"] is False
    assert inspect_manifest(included.manifest)["activities"] == summary["activities"]


def test_referenced_workspace_rules() -> None:
    """Relative references are refused; the two forms cannot be mixed."""
    with pytest.raises(ManifestError, match="absolute IRI"):
        create_from_hdf5(workspace_bytes(), workspace_ref="workspace-copy.h5")
    data = create_from_hdf5(workspace_bytes(), date_published=DATE, workspace_ref=REF)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = archive.read(MANIFEST_NAME)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_NAME, manifest)
        archive.writestr("workspace.h5", workspace_bytes())
    with pytest.raises(CapsuleError, match="referenced workspace"):
        read_capsule(buffer.getvalue())
