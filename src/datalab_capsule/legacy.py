# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Provenance of workspaces saved without a ledger (legacy workspaces).

DataLab Desktop stores, in each processed object's metadata, a record of the
processing that produced it (``__processing_parameters``) and of the last analysis
run on it (``__analysis_parameters``). When a workspace is opened, such records of
objects unknown to the ledger become ``legacy_metadata`` activities.

The record is kept as stored and nothing missing is invented:

- inputs are bound to the recorded source objects, whose state at execution time
  is unknown (never to their current state);
- the call is opaque and has no parameters: the stored parameter text is kept in
  the record, read as plain JSON (no class is imported) when possible;
- environment, edition, implementation and times are unknown (null);
- the object's current state is the output, observed when the workspace was
  opened: the record does not prove that the object was not changed since;
- a legacy activity is never replayed.

Issues found at import are listed in each activity (``legacy.issues``):

- ``missing_ancestry``: the record names no valid source object;
- ``unavailable_input``: a recorded source object is not in the workspace;
- ``unsupported_operation``: the recorded operation is unknown to the application;
- ``pattern_unknown``: the processing pattern is not one of the known ones;
- ``parameters_unreadable``: the stored parameter text is not a JSON object;
- ``result_not_identified``: an analysis whose result key was not recorded;
- ``output_not_observed``: the object's state could not be observed;
- ``record_unreadable``: the record is not a mapping;
- ``version_unknown``: the file does not say which version saved it;
- ``version_mismatch``: the file was saved by another version than the current one.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from datalab_capsule.integrity import _json_ready
from datalab_capsule.ledger import Ledger

__all__ = [
    "ANALYSIS_KEY",
    "LEGACY_LIMIT",
    "PROCESSING_KEY",
    "LegacyObject",
    "import_legacy",
    "legacy_summary",
]

PROCESSING_KEY = "__processing_parameters"
ANALYSIS_KEY = "__analysis_parameters"
#: Limit of every legacy activity: input states, parameters, environment and time
#: of the execution are not established.
LEGACY_LIMIT = "legacy_context_unknown"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DATASET_METADATA = ("class_module", "class_name", "schema_version")


@dataclasses.dataclass(frozen=True)
class LegacyObject:
    """An object of an opened workspace.

    Attributes:
        object_uuid: Persistent object identifier.
        kind: ``signal`` or ``image``.
        metadata: Object metadata (read only).
        facts: State facts observed at opening, or None if they could not be.
    """

    object_uuid: str
    kind: str
    metadata: Mapping[str, Any]
    facts: dict[str, Any] | None


def _roles(
    pattern: Any, record: Mapping[str, Any]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return the ``(role, object_uuid)`` inputs of a record and its issues."""
    issues: list[str] = []
    uuids = record.get("source_uuids")
    if isinstance(uuids, list) and uuids:
        sources = list(uuids)
    elif record.get("source_uuid") is not None:
        sources = [record.get("source_uuid")]
    else:
        sources = []
    valid = [u for u in sources if isinstance(u, str) and _UUID.match(u)]
    if not valid or len(valid) != len(sources):
        issues.append("missing_ancestry")
    if pattern in ("1-to-1", "1-to-n", "1-to-0") and len(valid) == 1:
        roles = ["source"]
    elif pattern == "2-to-1" and len(valid) == 2:
        roles = ["source", "operand"]
    elif pattern == "n-to-1":
        roles = ["sources"] * len(valid)
    else:
        if pattern not in ("1-to-1", "1-to-n", "1-to-0", "2-to-1", "n-to-1"):
            issues.append("pattern_unknown")
        roles = ["source" if len(valid) == 1 else "sources"] * len(valid)
    return list(zip(roles, valid)), issues


def _parameters(text: Any) -> tuple[Any, list[str]]:
    """Read stored parameter text as plain JSON (no class is imported)."""
    texts = text if isinstance(text, list) else [text]
    read = []
    for item in texts:
        try:
            payload = json.loads(item) if isinstance(item, str) else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return None, ["parameters_unreadable"]
        read.append(
            {
                "class": ".".join(
                    str(payload.get(k)) for k in ("class_module", "class_name")
                ),
                "values": {
                    k: v for k, v in payload.items() if k not in _DATASET_METADATA
                },
            }
        )
    return (read if isinstance(text, list) else read[0]), []


def _legacy_record(
    obj: LegacyObject,
    key: str,
    present: set[str],
    is_known: Callable[[str, str, Any], bool],
    version_issues: list[str],
    saved_version: str | None,
) -> tuple[list[tuple[str, str]], dict[str, Any], bool]:
    """Return the inputs, the legacy description and whether a state is output."""
    record = obj.metadata[key]
    if not isinstance(record, Mapping):
        legacy = {
            "key": key,
            "object_uuid": obj.object_uuid,
            "record": str(record),
            "saved_by_version": saved_version,
            "issues": ["record_unreadable", *version_issues],
        }
        return [], legacy, key == PROCESSING_KEY
    pattern = record.get("pattern")
    inputs, issues = _roles(pattern, record)
    if any(uuid_ not in present for _role, uuid_ in inputs):
        issues.append("unavailable_input")
    func_name = record.get("func_name")
    if not isinstance(func_name, str) or not is_known(
        obj.kind, func_name, record.get("plugin_origin")
    ):
        issues.append("unsupported_operation")
    legacy = {
        "key": key,
        "object_uuid": obj.object_uuid,
        "record": _json_ready(dict(record)),
        "saved_by_version": saved_version,
        "issues": [],
    }
    if "param_json" in record:
        parameters, parameter_issues = _parameters(record["param_json"])
        issues += parameter_issues
        if parameters is not None:
            legacy["parameters"] = _json_ready(parameters)
    if key == ANALYSIS_KEY:
        issues.append("result_not_identified")
    legacy["issues"] = issues + version_issues
    return inputs, legacy, key == PROCESSING_KEY


def import_legacy(
    ledger: Ledger,
    objects: Iterable[LegacyObject],
    *,
    is_known: Callable[[str, str, Any], bool],
    saved_version: str | None,
    current_version: str | None,
) -> list[dict[str, Any]]:
    """Record the legacy processing records of objects unknown to the ledger.

    An object is known when it has a state or already holds an imported record,
    so that reopening a workspace never imports a record twice.

    Args:
        ledger: Ledger of the opened workspace.
        objects: Every object of the workspace, in workspace order.
        is_known: ``(kind, operation name, plugin origin) -> bool``: whether the
         application knows the recorded operation.
        saved_version: Version that saved the file, as stored, or None.
        current_version: Version of this application, comparable with
         *saved_version*, or None when the two cannot be compared.

    Returns:
        The recorded activities.
    """
    objects = list(objects)
    present = {obj.object_uuid for obj in objects}
    known = set(ledger.object_uuids()) | {
        activity["legacy"]["object_uuid"]
        for activity in ledger.activities
        if activity["origin"] == "legacy_metadata"
    }
    if saved_version is None:
        version_issues = ["version_unknown"]
    elif current_version is not None and saved_version != current_version:
        version_issues = ["version_mismatch"]
    else:
        version_issues = []
    recorded = []
    for obj in objects:
        if obj.object_uuid in known:
            continue
        for key in (PROCESSING_KEY, ANALYSIS_KEY):
            if key not in obj.metadata:
                continue
            inputs, legacy, has_output = _legacy_record(
                obj, key, present, is_known, version_issues, saved_version
            )
            outputs = []
            if has_output:
                if obj.facts is None:
                    legacy["issues"].append("output_not_observed")
                else:
                    outputs.append(("result", obj.object_uuid, obj.facts))
            recorded.append(
                ledger.record_legacy_activity(
                    inputs=inputs, outputs=outputs, legacy=legacy, limits=[LEGACY_LIMIT]
                )
            )
    return recorded


def legacy_summary(ledger: Ledger) -> dict[str, Any]:
    """Summarise the legacy activities of a ledger and the issues found at import.

    Returns:
        ``{"activities", "issues", "items"}``: number of legacy activities, count
        of each issue, and one ``{"activity_id", "object_uuid", "key",
        "operation", "pattern", "inputs", "issues"}`` item per activity
        (``object_uuid`` is the object holding the record, ``inputs`` the
        recorded source objects).
    """
    items = []
    counts: dict[str, int] = {}
    for activity in ledger.activities:
        if activity["origin"] != "legacy_metadata":
            continue
        legacy = activity["legacy"]
        record = legacy["record"] if isinstance(legacy["record"], dict) else {}
        for issue in legacy["issues"]:
            counts[issue] = counts.get(issue, 0) + 1
        items.append(
            {
                "activity_id": activity["activity_id"],
                "object_uuid": legacy.get("object_uuid"),
                "key": legacy["key"],
                "operation": record.get("func_name"),
                "pattern": record.get("pattern"),
                "inputs": [
                    i["binding"]["object_uuids"][0] for i in activity["call"]["inputs"]
                ],
                "issues": list(legacy["issues"]),
            }
        )
    return {"activities": len(items), "issues": counts, "items": items}
