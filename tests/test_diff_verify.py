# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for the semantic diff and the verification of capsules (no computation)."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from datalab_capsule.archive import create_from_hdf5, read_capsule
from datalab_capsule.cli import main
from datalab_capsule.compare import build_report, compare_environments
from datalab_capsule.diff import diff_manifests
from datalab_capsule.hdf5 import read_block
from datalab_capsule.integrity import sha256_digest, value_digest
from datalab_capsule.ledger import Ledger, LedgerError
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    WORKSPACE_NAME,
    build_manifest,
    canonical_json,
    manifest_fingerprint,
)
from datalab_capsule.review import create_review
from datalab_capsule.verify import verify_capsule

from .test_capsule import DATE

CHAIN = Path(__file__).parent / "fixtures" / "workspaces" / "desktop_chain.h5"
SOURCES = Path(__file__).parent / "fixtures" / "corpus" / "sources"
REF = "https://data.example.org/workspace.h5"


def _read(path: Path) -> tuple[Ledger, dict]:
    with h5py.File(path, "r") as h5file:
        return read_block(h5file)


def _scientific_only(ledger: Ledger, record: dict, locators: dict) -> set:
    """Fields changed between a ledger and an edited copy; all scientific."""
    diff = diff_manifests(
        _manifest(ledger, locators), _manifest(Ledger.from_dict(record), locators)
    )
    assert diff["presentation"] == diff["environment"] == diff["storage"] == []
    return {(c["item"], c["field"]) for c in diff["scientific"]}


def _chain() -> tuple[bytes, Ledger, dict]:
    data = CHAIN.read_bytes()
    with h5py.File(io.BytesIO(data), "r") as h5file:
        ledger, locators = read_block(h5file)
    return data, ledger, locators


def _manifest(ledger: Ledger, locators: dict, **kwargs) -> dict:
    kwargs.setdefault("date_published", DATE)
    return build_manifest(
        ledger,
        locators,
        workspace_sha256="sha256:" + "0" * 64,
        workspace_size=1,
        **kwargs,
    )


def _zip(manifest: dict, workspace: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(MANIFEST_NAME, canonical_json(manifest))
        archive.writestr(WORKSPACE_NAME, workspace)
    return buffer.getvalue()


def test_identical_manifests() -> None:
    """Two identical manifests have no change."""
    _data, ledger, locators = _chain()
    manifest = _manifest(ledger, locators)
    diff = diff_manifests(manifest, copy.deepcopy(manifest))
    assert diff["identical"] and not diff["revision_of_old"]
    assert diff["scientific_conclusion"] == "none"
    assert diff["old"] == diff["new"] == manifest_fingerprint(manifest)


def test_revision_with_changed_parameter() -> None:
    """A parameter change in a revision is a scientific change of that activity."""
    _data, ledger, locators = _chain()
    parent = _manifest(ledger, locators)
    record = ledger.to_dict()
    activity = record["activities"][0]
    activity["call"]["parameters"]["value"] = 2.0
    child = _manifest(
        Ledger.from_dict(record), locators, revision_of=manifest_fingerprint(parent)
    )
    diff = diff_manifests(parent, child)
    assert diff["revision_of_old"] and not diff["identical"]
    assert diff["scientific_conclusion"] == "differences"
    assert diff["scientific"] == [
        {
            "change": "changed",
            "item": "activity",
            "id": f"#activity-{activity['activity_id']}",
            "field": "parameters",
            "old": {"value": 1.0},
            "new": {"value": 2.0},
        }
    ]
    assert diff["presentation"] == diff["environment"] == []


def test_presentation_and_storage_changes() -> None:
    """Name is presentation; locators and workspace storage are storage."""
    _data, ledger, locators = _chain()
    old = _manifest(ledger, locators)
    moved = {k: {**v, "path": v["path"] + "_moved"} for k, v in locators.items()}
    new = _manifest(ledger, moved, name="Renamed", workspace_ref=REF)
    diff = diff_manifests(old, new)
    assert diff["scientific"] == diff["environment"] == []
    assert {(c["item"], c["field"]) for c in diff["presentation"]} == {
        ("capsule", "name")
    }
    assert {(c["item"], c["field"]) for c in diff["storage"]} == {
        ("state", "locator"),
        ("workspace", "id"),
        ("workspace", "referenced"),
    }
    unlocated = _manifest(ledger, {})
    assert {c["field"] for c in diff_manifests(old, unlocated)["storage"]} == {
        "locator"
    }


def test_resaved_workspace_is_not_a_scientific_change() -> None:
    """Saving the same content again changes the bytes, not the science.

    The comparison reports the new file under storage and does not conclude
    that the workspaces are scientifically equal: only the recorded activities
    and states were compared. Fingerprints are those of the manifests.
    """
    data = CHAIN.read_bytes()
    copy_ = io.BytesIO()
    with h5py.File(io.BytesIO(data), "r") as source, h5py.File(copy_, "w") as target:
        for key, value in source.attrs.items():
            target.attrs[key] = value
        for name in source:
            source.copy(source[name], target, name=name)
    resaved = copy_.getvalue()
    assert resaved != data
    old = read_capsule(create_from_hdf5(data, date_published=DATE)).manifest
    new = read_capsule(create_from_hdf5(resaved, date_published=DATE)).manifest
    assert verify_capsule(create_from_hdf5(resaved, date_published=DATE))["valid"]
    diff = diff_manifests(old, new)
    assert diff["scientific"] == diff["presentation"] == []
    assert diff["environment"] == diff["attachments"] == []
    assert {c["field"] for c in diff["storage"]} == {"sha256", "size"}
    assert not diff["identical"]
    assert diff["scientific_conclusion"] == "none_detected"
    assert diff["old"] == manifest_fingerprint(old) != manifest_fingerprint(new)


def test_attachment_contents_withhold_equality() -> None:
    """Same workspace, different attachment contents: equality is not concluded.

    An attachment such as a calibration certificate may matter scientifically
    and is not interpreted. Its description or media type alone does not.
    """
    data = CHAIN.read_bytes()
    note = {"name": "certificate.txt", "data": b"Gain 1.02", "media_type": "text/plain"}

    def manifest(*attachments: dict) -> dict:
        capsule = create_from_hdf5(
            data, date_published=DATE, attachments=list(attachments)
        )
        return read_capsule(capsule).manifest

    plain, certified = manifest(), manifest(note)
    recertified = manifest({**note, "data": b"Gain 1.03"})
    described = manifest({**note, "description": "Calibration certificate"})
    for old, new in ((plain, certified), (certified, plain), (certified, recertified)):
        diff = diff_manifests(old, new)
        assert diff["scientific"] == diff["storage"] == []
        assert diff["attachments"]
        assert diff["scientific_conclusion"] == "none_detected"
    diff = diff_manifests(certified, described)
    assert [c["field"] for c in diff["attachments"]] == ["description"]
    assert diff["scientific_conclusion"] == "none"


def test_roi_change_is_scientific() -> None:
    """A different ROI on the same data is a scientific change of the state."""
    ledger, locators = _read(SOURCES / "capture_desktop.h5")
    record = ledger.to_dict()
    state = next(s for s in record["states"].values() if s.get("roi"))
    definition = copy.deepcopy(state["roi"]["definition"])
    definition["single_rois"][0]["coords"] = [0.0, 0.0, 1.0, 1.0]
    state["roi"] = {"definition": definition, "digest": value_digest(definition)}
    assert _scientific_only(ledger, record, locators) == {("state", "roi")}


def test_alignment_context_change_is_scientific() -> None:
    """A different X-alignment rule is a scientific change of the activity."""
    ledger, locators = _read(SOURCES / "difference_desktop.h5")
    record = ledger.to_dict()
    record["activities"][0]["context"]["x_alignment"]["interpolated"] = False
    assert _scientific_only(ledger, record, locators) == {("activity", "x_alignment")}


def test_analysis_result_change_is_scientific() -> None:
    """A different recorded analysis result is a scientific change."""
    ledger, locators = _read(SOURCES / "capture_desktop.h5")
    record = ledger.to_dict()
    output = next(
        o for a in record["activities"] for o in a["outputs"] if "artifact" in o
    )
    output["artifact"]["digest"] = "sha256:" + "1" * 64
    assert _scientific_only(ledger, record, locators) == {("activity", "outputs")}


def test_environment_change() -> None:
    """A package version change is an environment change, not a scientific one."""
    _data, ledger, locators = _chain()
    old = _manifest(ledger, locators)
    record = ledger.to_dict()
    env_id, env = next(iter(record["environments"].items()))
    env = copy.deepcopy(env)
    env["packages"]["numpy"] = "0.0.0"
    record["environments"][env_id] = env
    with pytest.raises(LedgerError):
        Ledger.from_dict(record)  # environment ids are content digests
    new_ledger = Ledger.from_dict(ledger.to_dict())
    new_id = new_ledger.add_environment(env)
    record = new_ledger.to_dict()
    for activity in record["activities"]:
        if activity["environment_id"] == env_id:
            activity["environment_id"] = new_id
    del record["environments"][env_id]
    diff = diff_manifests(old, _manifest(Ledger.from_dict(record), locators))
    assert diff["scientific"] == diff["presentation"] == []
    items = {(c["change"], c["item"]) for c in diff["environment"]}
    assert items == {
        ("added", "environment"),
        ("removed", "environment"),
        ("changed", "activity"),
    }


def test_verify_valid_capsule() -> None:
    """A capsule built from its workspace verifies without any computation."""
    data, ledger, locators = _chain()
    result = verify_capsule(create_from_hdf5(data, date_published=DATE))
    assert result["valid"], result["problems"]
    assert result["workspace"] == "verified"
    assert result["states_located"] == len(locators) > 0
    assert result["states_altered"] == []


def test_verify_detects_altered_state() -> None:
    """A stored state changed after the last save is reported as altered."""
    data, _ledger, locators = _chain()
    state_id, locator = next(iter(locators.items()))
    buffer = io.BytesIO(data)
    with h5py.File(buffer, "r+") as h5file:
        dataset = h5file[locator["path"]]["xydata"]
        values = dataset[()]
        values[1, 0] += 1.0
        dataset[...] = values
    result = verify_capsule(create_from_hdf5(buffer.getvalue(), date_published=DATE))
    assert not result["valid"]
    assert result["states_altered"] == [state_id]


def test_verify_detects_manifest_not_matching_ledger() -> None:
    """A valid manifest that does not describe the workspace ledger is refused."""
    data, ledger, locators = _chain()
    record = ledger.to_dict()
    record["activities"][0]["call"]["parameters"]["value"] = 2.0
    manifest = build_manifest(
        Ledger.from_dict(record),
        locators,
        workspace_sha256=sha256_digest(data),
        workspace_size=len(data),
        date_published=DATE,
    )
    result = verify_capsule(_zip(manifest, data))
    assert result["problems"] == ["The manifest does not describe the workspace ledger"]


def test_verify_referenced_workspace_and_documents() -> None:
    """Referenced workspaces, reports and reviews are checked only when supplied."""
    data, ledger, _locators = _chain()
    capsule = create_from_hdf5(data, date_published=DATE, workspace_ref=REF)
    result = verify_capsule(capsule)
    assert result["valid"] and result["workspace"] == "not_available"
    assert result["states_located"] is None
    manifest = read_capsule(capsule).manifest
    activity = ledger.activities[0]
    env = compare_environments(None, None, None, None)
    good = build_report(
        activity_id=activity["activity_id"],
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference=None,
        environment=env,
    )
    stranger = {**good, "activity_id": "00000000-0000-4000-8000-0000000000ff"}
    review = create_review(manifest, reviewer="A. Reviewer", statement="Checked.")
    other = create_review("sha256:" + "1" * 64, reviewer="B", statement="Other.")
    result = verify_capsule(
        capsule,
        resolve_workspace=lambda _iri: data,
        reports=[good, stranger],
        reviews=[review, other],
    )
    assert result["workspace"] == "verified"
    assert [r["status"] for r in result["reports"]] == [
        "valid",
        "The report names an activity absent from the capsule",
    ]
    assert result["reviews"][0]["status"] == "valid"
    assert result["reviews"][1]["status"] != "valid"
    assert len(result["problems"]) == 2 and not result["valid"]


def test_cli_diff_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The command line compares and verifies capsules, with exit codes."""
    data, _ledger, _locators = _chain()
    old = tmp_path / "old.zip"
    new = tmp_path / "new.zip"
    old.write_bytes(create_from_hdf5(data, date_published=DATE))
    parent = manifest_fingerprint(read_capsule(old).manifest)
    new.write_bytes(
        create_from_hdf5(data, date_published=DATE, name="Renamed", revision_of=parent)
    )
    assert main(["diff", str(old), str(old)]) == 0
    assert "Identical." in capsys.readouterr().out
    assert main(["diff", str(old), str(new)]) == 1
    out = capsys.readouterr().out
    assert "New is a revision of old." in out and "Presentation changes (1)" in out
    assert main(["diff", str(old), str(new), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["revision_of_old"] is True
    assert main(["verify", str(old)]) == 0
    assert "Verified." in capsys.readouterr().out
    referenced = tmp_path / "ref.zip"
    referenced.write_bytes(
        create_from_hdf5(data, date_published=DATE, workspace_ref=REF)
    )
    workspace = tmp_path / "workspace.h5"
    workspace.write_bytes(data)
    assert (
        main(["verify", str(referenced), "--workspace", str(workspace), "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["workspace"] == "verified"
    workspace.write_bytes(np.zeros(4).tobytes())
    assert main(["verify", str(referenced), "--workspace", str(workspace)]) == 1
    assert "Problem: Invalid capsule" in capsys.readouterr().out


def test_verify_without_h5py(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without h5py, only the workspace integrity is checked, and said so."""
    data, _ledger, _locators = _chain()
    capsule = create_from_hdf5(data, date_published=DATE)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args: None if name == "h5py" else real_find_spec(name, *args),
    )
    result = verify_capsule(capsule)
    assert result["valid"] and result["workspace"] == "integrity_only"
    assert result["states_located"] is None
