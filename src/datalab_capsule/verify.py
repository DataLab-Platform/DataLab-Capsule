# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Verification of a capsule without any computation.

:func:`verify_capsule` checks, and only reads:

- the archive and its manifest (safe reading, schema, fingerprint);
- the workspace: its SHA-256 and size, then, with h5py, its provenance block: the
  manifest must describe exactly that ledger, and every stored state must still
  match its fingerprint and ROI;
- supplied verification reports (schema, activity present in the capsule);
- supplied review assertions (schema, own fingerprint, target revision).

A referenced workspace is checked only when the caller passes a resolver.
Recomputing an activity is the editions' job (replay), never this module's.
"""

from __future__ import annotations

import importlib.util
import io
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from datalab_capsule.archive import DEFAULT_SIZE_POLICY, SizePolicy, read_capsule
from datalab_capsule.compare import ReportError, validate_report
from datalab_capsule.integrity import ProvenanceError, sha256_digest
from datalab_capsule.manifest import (
    REFERENCED_KEY,
    REVISION_KEY,
    _entities,
    build_manifest,
    manifest_fingerprint,
    workspace_entity,
)
from datalab_capsule.review import ReviewError, validate_review

__all__ = ["verify_capsule"]


def _workspace_checks(manifest: Mapping[str, Any], workspace: bytes) -> dict[str, Any]:
    """Check that the workspace ledger and stored states match the manifest."""
    import h5py  # pylint: disable=import-outside-toplevel

    from datalab_capsule.hdf5 import load_ledger, read_block

    problems: list[str] = []
    with h5py.File(io.BytesIO(workspace), "r") as h5file:
        block = read_block(h5file)
        if block is None:
            return {"problems": ["The workspace has no provenance block"]}
        ledger, locators = block
        _ledger, status = load_ledger(h5file)
    entities = _entities(manifest)
    root = entities["./"]
    entity = workspace_entity(manifest)
    rebuilt = build_manifest(
        ledger,
        locators,
        workspace_sha256=sha256_digest(workspace),
        workspace_size=len(workspace),
        name=root["name"],
        description=root["description"],
        license_=root["license"],
        date_published=root["datePublished"],
        revision_of=root.get(REVISION_KEY),
        workspace_ref=entity["@id"] if entity.get(REFERENCED_KEY) else None,
    )
    if manifest_fingerprint(rebuilt) != manifest_fingerprint(manifest):
        problems.append("The manifest does not describe the workspace ledger")
    altered = sorted(s for s, value in status.items() if value == "altered")
    problems.extend(f"Stored state altered: {state_id}" for state_id in altered)
    return {
        "states_located": len(locators),
        "states_altered": altered,
        "problems": problems,
    }


def verify_capsule(
    source: Any,
    *,
    resolve_workspace: Callable[[str], bytes] | None = None,
    reports: Iterable[Mapping[str, Any]] = (),
    reviews: Iterable[Mapping[str, Any]] = (),
    policy: SizePolicy = DEFAULT_SIZE_POLICY,
) -> dict[str, Any]:
    """Verify a capsule, its workspace and supplied documents; compute nothing.

    Args:
        source: Path or bytes of the capsule.
        resolve_workspace: ``IRI -> bytes`` for a referenced workspace.
        reports: Verification reports to check against the capsule.
        reviews: Review assertions to check against the capsule.
        policy: Size limits.

    Returns:
        ``{"valid", "fingerprint", "workspace", "states_located",
        "states_altered", "reports", "reviews", "problems"}``. ``workspace`` is
        ``verified``, ``integrity_only`` (SHA-256 and size checked, h5py not
        installed) or ``not_available`` (referenced, no resolver); ``valid`` is
        True when no problem was found. Each report or review entry gives its
        ``status`` (``valid`` or the problem).
    """
    result: dict[str, Any] = {
        "valid": False,
        "fingerprint": None,
        "workspace": None,
        "states_located": None,
        "states_altered": [],
        "reports": [],
        "reviews": [],
        "problems": [],
    }
    try:
        capsule = read_capsule(source, policy, resolve_workspace)
    except (OSError, ProvenanceError) as exc:
        result["problems"].append(f"Invalid capsule: {exc}")
        return result
    manifest = capsule.manifest
    result["fingerprint"] = manifest_fingerprint(manifest)
    if capsule.workspace is None:
        result["workspace"] = "not_available"
    elif importlib.util.find_spec("h5py") is None:
        result["workspace"] = "integrity_only"
    else:
        try:
            checks = _workspace_checks(manifest, capsule.workspace)
        except (OSError, ProvenanceError) as exc:
            checks = {"problems": [f"Invalid workspace: {exc}"]}
        result["workspace"] = "verified"
        result["states_located"] = checks.get("states_located")
        result["states_altered"] = checks.get("states_altered", [])
        result["problems"].extend(checks["problems"])
    activity_ids = {
        key.split("#activity-", 1)[1]
        for key in _entities(manifest)
        if key.startswith("#activity-")
    }
    for report in reports:
        status = "valid"
        try:
            validate_report(report)
            if report["activity_id"] not in activity_ids:
                status = "The report names an activity absent from the capsule"
        except (ReportError, KeyError, TypeError) as exc:
            status = f"Invalid report: {exc}"
        result["reports"].append(
            {
                "activity_id": report.get("activity_id")
                if isinstance(report, Mapping)
                else None,
                "verdict": report.get("verdict")
                if isinstance(report, Mapping)
                else None,
                "status": status,
            }
        )
    for review in reviews:
        status = "valid"
        try:
            validate_review(review, manifest)
            cited = {r["activity_id"] for r in review["reports"]}
            if not cited <= activity_ids:
                status = (
                    "The review cites a report on an activity absent from the capsule"
                )
        except ReviewError as exc:
            status = str(exc)
        result["reviews"].append(
            {
                "fingerprint": review.get("review_fingerprint")
                if isinstance(review, Mapping)
                else None,
                "status": status,
            }
        )
    result["problems"].extend(
        f"{entry['status']}"
        for entry in result["reports"] + result["reviews"]
        if entry["status"] != "valid"
    )
    result["valid"] = not result["problems"]
    return result
