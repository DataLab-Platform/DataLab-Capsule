# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Semantic difference between two capsule manifests.

Changes are sorted into five groups:

- ``scientific``: activities (operation, parameters, inputs and outputs with
  analysis-result digests, origin, replayability, limits, X alignment) and
  states (kind, object, fingerprint, ROI, producer);
- ``presentation``: name, description, licence and publication date of the
  capsule;
- ``storage``: the workspace file (SHA-256, size, included or referenced,
  address) and where states are stored (locators). Different bytes do not show
  a scientific change: saving the same content again gives another file;
- ``environment``: execution environments (editions, versions, platforms,
  packages) and the environment each activity ran in;
- ``attachments``: attachments added, removed or changed (SHA-256, size, media
  type, description). Their content is not interpreted: a protocol or a
  calibration certificate may matter scientifically, so the reader judges.

``scientific_conclusion`` states what the comparison can conclude, without
claiming more than the manifests cover: ``"differences"`` (scientific changes
found), ``"none"`` (no scientific change and the same workspace bytes) or
``"none_detected"`` (no change in the recorded activities and states, but the
workspace files differ: content outside the recorded provenance is not
compared, so equality is not established).

Activities and states are matched by their identifiers, which a revision keeps
for everything it inherits. Nothing is computed; only the manifests are read.
Fingerprints and identities are not affected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datalab_capsule.manifest import (
    REVISION_KEY,
    _entities,
    inspect_manifest,
    manifest_fingerprint,
)

__all__ = ["diff_manifests"]

_ROOT_FIELDS = ("name", "description", "license", "date_published")
_ACTIVITY_FIELDS = (
    "name",
    "type",
    "replayable",
    "instrument",
    "origin",
    "inputs",
    "outputs",
    "parameters",
    "limits",
    "x_alignment",
    "legacy",
    "observed_state",
)
_STATE_SCIENTIFIC = ("kind", "object_uuid", "fingerprint", "roi", "produced_by")


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def _compare(
    changes: list[dict[str, Any]],
    item: str,
    old: Mapping[str, dict[str, Any]],
    new: Mapping[str, dict[str, Any]],
    fields: tuple[str, ...],
    added_removed: bool = True,
) -> None:
    for key in sorted(set(old) | set(new)):
        if key not in new or key not in old:
            if added_removed:
                kind = "removed" if key not in new else "added"
                changes.append({"change": kind, "item": item, "id": key})
            continue
        for field in fields:
            if old[key].get(field) != new[key].get(field):
                changes.append(
                    {
                        "change": "changed",
                        "item": item,
                        "id": key,
                        "field": field,
                        "old": old[key].get(field),
                        "new": new[key].get(field),
                    }
                )


def _environments(manifest: Any) -> dict[str, dict[str, Any]]:
    """Return the environments of a manifest, packages resolved to versions."""
    entities = _entities(manifest)
    result = {}
    for entity in entities.values():
        if entity.get("@type") != "dlc:Environment":
            continue
        packages = {}
        for ref in entity.get("softwareRequirements") or []:
            package = entities.get(ref.get("@id"), {})
            packages[package.get("name")] = package.get("softwareVersion")
        result[entity["@id"]] = {
            "edition": entity.get("dlc:edition"),
            "edition_version": entity.get("dlc:editionVersion"),
            "python": entity.get("dlc:python"),
            "platform": entity.get("dlc:platform"),
            "pyodide": entity.get("dlc:pyodide"),
            "packages": packages,
        }
    return result


def _activity_environments(manifest: Any) -> dict[str, dict[str, Any]]:
    """Return the environment each activity ran in."""
    return {
        key: {"environment": (entity.get("dlc:environment") or {}).get("@id")}
        for key, entity in _entities(manifest).items()
        if key.startswith("#activity-")
    }


def diff_manifests(old: Any, new: Any) -> dict[str, Any]:
    """Return the semantic difference between two capsule manifests.

    Args:
        old: Earlier manifest (e.g. the parent revision).
        new: Later manifest.

    Returns:
        ``{"old", "new", "revision_of_old", "identical",
        "scientific_conclusion", "scientific", "presentation", "storage",
        "environment", "attachments"}``: the two manifest
        fingerprints, whether *new* names *old* as its parent, whether nothing
        differs, what can be concluded scientifically, and the changes of each
        group. A change is ``{"change":
        "added" | "removed" | "changed", "item", "id"}``, plus ``"field"``,
        ``"old"`` and ``"new"`` for a changed field.
    """
    old_summary, new_summary = inspect_manifest(old), inspect_manifest(new)
    scientific: list[dict[str, Any]] = []
    presentation: list[dict[str, Any]] = []
    storage: list[dict[str, Any]] = []
    environment: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    _compare(
        scientific,
        "activity",
        _by_id(old_summary["activities"]),
        _by_id(new_summary["activities"]),
        _ACTIVITY_FIELDS,
    )
    old_states = _by_id(old_summary["states"])
    new_states = _by_id(new_summary["states"])
    _compare(scientific, "state", old_states, new_states, _STATE_SCIENTIFIC)
    _compare(storage, "state", old_states, new_states, ("locator",), False)
    old_ws, new_ws = old_summary["workspace"], new_summary["workspace"]
    _compare(
        storage,
        "workspace",
        {"workspace": old_ws},
        {"workspace": new_ws},
        ("sha256", "size", "id", "referenced"),
    )
    _compare(
        presentation,
        "capsule",
        {"capsule": old_summary},
        {"capsule": new_summary},
        _ROOT_FIELDS,
    )
    _compare(
        attachments,
        "attachment",
        {a["name"]: a for a in old_summary["attachments"]},
        {a["name"]: a for a in new_summary["attachments"]},
        ("sha256", "size", "media_type", "description"),
    )
    _compare(
        environment,
        "environment",
        _environments(old),
        _environments(new),
        ("edition", "edition_version", "python", "platform", "pyodide", "packages"),
    )
    _compare(
        environment,
        "activity",
        _activity_environments(old),
        _activity_environments(new),
        ("environment",),
        False,
    )
    old_fp, new_fp = manifest_fingerprint(old), manifest_fingerprint(new)
    root = _entities(new)["./"]
    if scientific:
        conclusion = "differences"
    elif old_ws["sha256"] == new_ws["sha256"]:
        conclusion = "none"
    else:
        conclusion = "none_detected"
    groups = (scientific, presentation, storage, environment, attachments)
    return {
        "old": old_fp,
        "new": new_fp,
        "revision_of_old": root.get(REVISION_KEY) == old_fp,
        "identical": not any(groups),
        "scientific_conclusion": conclusion,
        "scientific": scientific,
        "presentation": presentation,
        "storage": storage,
        "environment": environment,
        "attachments": attachments,
    }
