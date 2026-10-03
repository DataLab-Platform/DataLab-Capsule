# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""RO-Crate manifest of a workspace capsule, built from its provenance ledger.

The manifest is an RO-Crate 1.3 metadata document that conforms to the Process
Run Crate 0.6 profile and to the experimental DataLab capsule profile 0.1:

- the root ``Dataset`` lists ``workspace.h5`` (a ``File`` with SHA-256 and size)
  and mentions one ``CreateAction`` (or ``UpdateAction`` for an in-place
  recomputation) per ledger activity;
- each action's ``instrument`` is a ``SoftwareApplication`` for the operation
  contract (replayable call) or for the informative implementation (opaque call,
  no operation identifier), plus one for the edition that ran it;
- each parameter is a ``PropertyValue``; each state is an entity carrying its
  fingerprint and, when present in ``workspace.h5``, its locator;
- JSON-LD arrays are unordered, so input and output roles are also listed as
  ``dlc:RoleBinding`` entities with an explicit ``position``.

The manifest fingerprint is the SHA-256 of the RFC 8785 canonical JSON of the
manifest without its ``dlc:manifestFingerprint`` property. It is never written
into the HDF5 file.

Terms of the DataLab profile use the ``dlc:`` prefix. :data:`PROFILE_IRI` and
:data:`TERMS_IRI` are placeholders until permanent identifiers are reserved.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

from datalab_capsule.integrity import ProvenanceError, canonical_json, json_digest
from datalab_capsule.ledger import Ledger, utc_timestamp

__all__ = [
    "MANIFEST_NAME",
    "PROCESS_RUN_CRATE",
    "PROFILE_IRI",
    "RO_CRATE",
    "RO_CRATE_CONTEXT",
    "TERMS_IRI",
    "WORKSPACE_NAME",
    "ManifestError",
    "build_manifest",
    "inspect_manifest",
    "load_ro_crate_context",
    "manifest_fingerprint",
    "validate_manifest",
]

MANIFEST_NAME = "ro-crate-metadata.json"
WORKSPACE_NAME = "workspace.h5"
RO_CRATE = "https://w3id.org/ro/crate/1.3"
RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.3/context"
PROCESS_RUN_CRATE = "https://w3id.org/ro/wfrun/process/0.6"
#: Placeholder IRI of the DataLab capsule profile (experimental, version 0.1).
PROFILE_IRI = "urn:datalab:capsule:profile:0.1"
#: Placeholder namespace of the DataLab capsule terms (prefix ``dlc:``).
TERMS_IRI = "urn:datalab:capsule:terms#"
FINGERPRINT_KEY = "dlc:manifestFingerprint"
EDITION_NAMES = {"desktop": "DataLab", "web": "DataLab-Web"}
#: Licence statement used when the author gives none (RO-Crate allows text).
DEFAULT_LICENSE = "No licence was specified for this workspace."


class ManifestError(ProvenanceError, ValueError):
    """Raised when a capsule manifest is invalid."""


def load_ro_crate_context() -> dict[str, Any]:
    """Return the embedded RO-Crate 1.3 JSON-LD context (CC0), without network."""
    text = (
        resources.files("datalab_capsule")
        .joinpath("contexts", "ro-crate-1.3.jsonld")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _ref(entity_id: str) -> dict[str, str]:
    return {"@id": entity_id}


def _json_value(value: Any) -> Any:
    """Return a JSON-LD literal for a parameter value (lossless JSON otherwise)."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return canonical_json(value)


def _activity_entities(
    activity: dict[str, Any], graph: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    act_id = f"#activity-{activity['activity_id']}"
    call = activity["call"]
    operation = call["operation"]
    instruments = []
    if operation is not None:
        action_name = operation["id"]
        op_id = f"#operation-{operation['id']}-v{operation['contract_version']}"
        graph.setdefault(
            op_id,
            {
                "@id": op_id,
                "@type": "SoftwareApplication",
                "name": operation["id"],
                "softwareVersion": str(operation["contract_version"]),
                "dlc:operationId": operation["id"],
                "dlc:contractVersion": operation["contract_version"],
            },
        )
        instruments.append(_ref(op_id))
    else:
        impl = activity.get("implementation") or {}
        name = impl.get("python_name") or "unknown"
        action_name = name
        impl_id = f"#implementation-{name}-{impl.get('version') or 'unknown'}"
        entity = {"@id": impl_id, "@type": "SoftwareApplication", "name": name}
        if impl.get("version"):
            entity["softwareVersion"] = impl["version"]
        if impl.get("package"):
            entity["dlc:package"] = impl["package"]
        graph.setdefault(impl_id, entity)
        instruments.append(_ref(impl_id))
    edition = activity["edition"]
    environment = graph.get(f"#environment-{activity['environment_id']}", {})
    version = environment.get("dlc:editionVersion", "unknown")
    edition_id = f"#edition-{edition}-{version}"
    graph.setdefault(
        edition_id,
        {
            "@id": edition_id,
            "@type": "SoftwareApplication",
            "name": EDITION_NAMES.get(edition, edition),
            "softwareVersion": version,
        },
    )
    instruments.append(_ref(edition_id))

    def bindings(items: list[dict[str, Any]], direction: str) -> list[dict[str, str]]:
        refs = []
        for position, item in enumerate(items):
            binding_id = f"{act_id}-{direction}-{position}"
            entity = {
                "@id": binding_id,
                "@type": "dlc:RoleBinding",
                "name": item["role"],
                "position": position,
            }
            state_id = item.get("state_id") or item.get("binding", {}).get("state_id")
            if state_id is not None:
                entity["dlc:state"] = _ref(f"#state-{state_id}")
            else:
                artifact_id = f"{act_id}-artifact-{position}"
                artifact = item["artifact"]
                graph[artifact_id] = {
                    "@id": artifact_id,
                    "@type": "CreativeWork",
                    "name": artifact["key"],
                    "dlc:artifactKind": artifact["kind"],
                    "dlc:objectUuid": artifact["object_uuid"],
                    "dlc:key": artifact["key"],
                }
                entity["dlc:artifact"] = _ref(artifact_id)
            graph[binding_id] = entity
            refs.append(_ref(binding_id))
        return refs

    inputs = bindings(call["inputs"], "input")
    outputs = bindings(activity["outputs"], "output")
    parameters = []
    if call["parameters"] is not None:
        for name, value in call["parameters"].items():
            param_id = f"{act_id}-parameter-{name}"
            entity = {
                "@id": param_id,
                "@type": "PropertyValue",
                "name": name,
                "dlc:jsonValue": canonical_json(value),
            }
            if value is not None:
                entity["value"] = _json_value(value)
            graph[param_id] = entity
            parameters.append(_ref(param_id))
    action: dict[str, Any] = {
        "@id": act_id,
        "@type": (
            "UpdateAction"
            if activity["origin"] == "recompute_in_place"
            else "CreateAction"
        ),
        "name": action_name,
        "instrument": instruments,
        "object": [graph[ref["@id"]]["dlc:state"] for ref in inputs],
        "result": [
            graph[ref["@id"]].get("dlc:state") or graph[ref["@id"]]["dlc:artifact"]
            for ref in outputs
        ],
        "actionStatus": _ref("http://schema.org/CompletedActionStatus"),
        "dlc:inputs": inputs,
        "dlc:outputs": outputs,
        "dlc:parameters": parameters,
        "dlc:replayable": operation is not None,
        "dlc:origin": activity["origin"],
        "dlc:environment": _ref(f"#environment-{activity['environment_id']}"),
        "dlc:limits": list(activity.get("limits") or []),
    }
    if activity.get("command_id"):
        action["dlc:commandId"] = activity["command_id"]
    if activity.get("started_at"):
        action["startTime"] = activity["started_at"]
    if activity.get("finished_at"):
        action["endTime"] = activity["finished_at"]
    graph[act_id] = action
    return action


def build_manifest(
    ledger: Ledger,
    locators: Mapping[str, Mapping[str, str]],
    *,
    workspace_sha256: str,
    workspace_size: int,
    name: str = "DataLab workspace",
    description: str = "DataLab workspace with its processing provenance.",
    license_: str = DEFAULT_LICENSE,
    date_published: str | None = None,
) -> dict[str, Any]:
    """Build the RO-Crate manifest of a capsule from a workspace's ledger.

    Args:
        ledger: Provenance ledger of the workspace.
        locators: ``state_id -> {"kind", "path"}`` of states stored in
         ``workspace.h5``.
        workspace_sha256: ``"sha256:<hex>"`` digest of ``workspace.h5``.
        workspace_size: Size of ``workspace.h5`` in bytes.
        name: Name of the capsule.
        description: Description of the capsule.
        license_: Licence statement (text or IRI); never guessed.
        date_published: ISO 8601 date (defaults to now).

    Returns:
        The manifest, with its fingerprint.
    """
    graph: dict[str, dict[str, Any]] = {}
    for env_id, env in ledger.environments.items():
        entity_id = f"#environment-{env_id}"
        packages = []
        for package, version in sorted((env.get("packages") or {}).items()):
            pkg_id = f"#package-{package}-{version}"
            graph.setdefault(
                pkg_id,
                {
                    "@id": pkg_id,
                    "@type": "SoftwareApplication",
                    "name": package,
                    "softwareVersion": str(version),
                },
            )
            packages.append(_ref(pkg_id))
        python = env.get("python") or {}
        graph[entity_id] = {
            "@id": entity_id,
            "@type": "dlc:Environment",
            "name": f"{env.get('edition')} environment",
            "identifier": env_id,
            "dlc:edition": env.get("edition"),
            "dlc:editionVersion": env.get("edition_version") or "unknown",
            "dlc:python": f"{python.get('implementation')} {python.get('version')}",
            "dlc:platform": env.get("platform"),
            "dlc:pyodide": env.get("pyodide"),
            "softwareRequirements": packages,
        }
    for state_id, state in ledger.states.items():
        entity_id = f"#state-{state_id}"
        fingerprint = state.get("fingerprint") or {}
        entity = {
            "@id": entity_id,
            "@type": "CreativeWork",
            "name": f"{state['kind']} state",
            "dlc:kind": state["kind"],
            "dlc:objectUuid": state["object_uuid"],
            "dlc:sequence": state.get("sequence"),
            "dlc:fingerprint": fingerprint.get("value"),
            "dlc:fingerprintScheme": fingerprint.get("scheme"),
        }
        locator = locators.get(state_id)
        if locator is not None:
            entity["dlc:locator"] = locator["path"]
            entity["isPartOf"] = _ref(WORKSPACE_NAME)
        if state.get("produced_by"):
            entity["dlc:producedBy"] = _ref(f"#activity-{state['produced_by']}")
        graph[entity_id] = entity
    actions = [_activity_entities(a, graph) for a in ledger.activities]
    root = {
        "@id": "./",
        "@type": "Dataset",
        "name": name,
        "description": description,
        "datePublished": date_published or utc_timestamp(),
        "license": license_,
        "conformsTo": [_ref(PROCESS_RUN_CRATE), _ref(PROFILE_IRI)],
        "hasPart": [_ref(WORKSPACE_NAME)],
        "mentions": [_ref(a["@id"]) for a in actions],
        "dlc:workspaceId": ledger.workspace_id,
    }
    head = [
        {
            "@id": MANIFEST_NAME,
            "@type": "CreativeWork",
            "about": _ref("./"),
            "conformsTo": _ref(RO_CRATE),
        },
        root,
        {
            "@id": PROCESS_RUN_CRATE,
            "@type": "CreativeWork",
            "name": "Process Run Crate",
            "version": "0.6",
        },
        {
            "@id": PROFILE_IRI,
            "@type": "CreativeWork",
            "name": "DataLab workspace capsule profile (experimental)",
            "version": "0.1",
        },
        {
            "@id": WORKSPACE_NAME,
            "@type": "File",
            "name": "DataLab HDF5 workspace",
            "encodingFormat": "application/x-hdf5",
            "contentSize": str(workspace_size),
            "sha256": workspace_sha256.split(":", 1)[-1],
        },
    ]
    manifest = {
        "@context": [RO_CRATE_CONTEXT, {"dlc": TERMS_IRI}],
        "@graph": head + [graph[key] for key in sorted(graph)],
    }
    root[FINGERPRINT_KEY] = manifest_fingerprint(manifest)
    return manifest


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Return the fingerprint of a manifest (its own fingerprint excluded)."""
    data = copy.deepcopy(dict(manifest))
    for entity in data.get("@graph", []):
        if isinstance(entity, dict) and entity.get("@id") == "./":
            entity.pop(FINGERPRINT_KEY, None)
    return json_digest(data)


def _entities(manifest: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("@graph"), list):
        raise ManifestError("The manifest must hold an @graph array")
    entities: dict[str, dict[str, Any]] = {}
    for entity in manifest["@graph"]:
        if not isinstance(entity, dict) or not isinstance(entity.get("@id"), str):
            raise ManifestError("Every graph entity must have a string @id")
        if entity["@id"] in entities:
            raise ManifestError(f"Duplicate entity {entity['@id']}")
        entities[entity["@id"]] = entity
    return entities


def _refs(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [item["@id"] for item in items if isinstance(item, dict) and "@id" in item]


def _is_local(ref: str) -> bool:
    return ref.startswith("#") or ref in ("./", WORKSPACE_NAME, MANIFEST_NAME)


def validate_manifest(manifest: Any) -> None:
    """Validate a capsule manifest offline (no JSON-LD processor, no network).

    Raises:
        ManifestError: Listing every problem found.
    """
    entities = _entities(manifest)
    problems: list[str] = []
    context = manifest.get("@context")
    if not isinstance(context, list) or RO_CRATE_CONTEXT not in context:
        problems.append("The @context must include the RO-Crate 1.3 context")
    descriptor = entities.get(MANIFEST_NAME, {})
    if _refs(descriptor.get("about")) != ["./"]:
        problems.append("The metadata descriptor must be about ./")
    if _refs(descriptor.get("conformsTo")) != [RO_CRATE]:
        problems.append("The metadata descriptor must conform to RO-Crate 1.3")
    root = entities.get("./", {})
    if root.get("@type") != "Dataset":
        problems.append("The root data entity must be a Dataset")
    for key in ("name", "description", "datePublished", "license"):
        if not root.get(key):
            problems.append(f"The root data entity has no {key}")
    profiles = _refs(root.get("conformsTo"))
    for profile in (PROCESS_RUN_CRATE, PROFILE_IRI):
        if profile not in profiles:
            problems.append(f"The root data entity does not conform to {profile}")
    if WORKSPACE_NAME not in _refs(root.get("hasPart")):
        problems.append(f"The root data entity does not contain {WORKSPACE_NAME}")
    workspace = entities.get(WORKSPACE_NAME, {})
    if workspace.get("@type") != "File" or not workspace.get("sha256"):
        problems.append(f"{WORKSPACE_NAME} must be a File with a SHA-256")
    try:
        defined = set(load_ro_crate_context()["@context"])
    except (OSError, KeyError, ValueError) as exc:  # pragma: no cover
        raise ManifestError(f"Embedded RO-Crate context unavailable: {exc}") from exc
    actions = [
        e
        for e in entities.values()
        if e.get("@type") in ("CreateAction", "UpdateAction")
    ]
    if sorted(_refs(root.get("mentions"))) != sorted(a["@id"] for a in actions):
        problems.append("The root data entity must mention every action")
    for entity in entities.values():
        types = entity.get("@type")
        for type_ in types if isinstance(types, list) else [types]:
            if not isinstance(type_, str) or (
                ":" not in type_ and type_ not in defined
            ):
                problems.append(f"{entity['@id']}: undefined type {type_!r}")
        for key, value in entity.items():
            if not key.startswith("@") and ":" not in key and key not in defined:
                problems.append(f"{entity['@id']}: undefined term {key!r}")
            for ref in _refs(value):
                if _is_local(ref) and ref not in entities:
                    problems.append(f"{entity['@id']}: dangling reference {ref}")
    for action in actions:
        operations = [
            entities[ref]
            for ref in _refs(action.get("instrument"))
            if ref in entities and "dlc:operationId" in entities[ref]
        ]
        if not _refs(action.get("instrument")):
            problems.append(f"{action['@id']}: no instrument")
        if action.get("dlc:replayable") is not bool(operations):
            problems.append(f"{action['@id']}: replayable flag and instrument differ")
        for key in ("dlc:inputs", "dlc:outputs"):
            positions = [
                entities[ref].get("position")
                for ref in _refs(action.get(key))
                if ref in entities
            ]
            if positions != list(range(len(positions))):
                problems.append(f"{action['@id']}: {key} positions are not ordered")
    fingerprint = root.get(FINGERPRINT_KEY)
    if fingerprint != manifest_fingerprint(manifest):
        problems.append("The manifest fingerprint does not match its content")
    if problems:
        raise ManifestError("; ".join(problems))


def _bound(entities: dict[str, dict[str, Any]], ref: str) -> dict[str, Any]:
    binding = entities[ref]
    target = binding.get("dlc:state") or binding.get("dlc:artifact")
    return {
        "role": binding["name"],
        "position": binding["position"],
        "target": target["@id"] if target else None,
    }


def inspect_manifest(manifest: Any) -> dict[str, Any]:
    """Summarise a manifest: activities, roles in order, states and their links."""
    entities = _entities(manifest)
    root = entities["./"]
    workspace = entities.get(WORKSPACE_NAME, {})
    activities = []
    consumers: dict[str, list[str]] = {}
    for ref in _refs(root.get("mentions")):
        action = entities[ref]
        inputs = [_bound(entities, r) for r in _refs(action.get("dlc:inputs"))]
        outputs = [_bound(entities, r) for r in _refs(action.get("dlc:outputs"))]
        for item in inputs:
            consumers.setdefault(item["target"], []).append(ref)
        tools = [entities[r] for r in _refs(action.get("instrument")) if r in entities]
        activities.append(
            {
                "id": ref,
                "type": action["@type"],
                "name": action.get("name"),
                "replayable": action.get("dlc:replayable"),
                "instrument": [t.get("name") for t in tools],
                "origin": action.get("dlc:origin"),
                "inputs": inputs,
                "outputs": outputs,
                "parameters": {
                    entities[p]["name"]: json.loads(entities[p]["dlc:jsonValue"])
                    for p in _refs(action.get("dlc:parameters"))
                },
                "limits": action.get("dlc:limits", []),
            }
        )
    states = [
        {
            "id": e["@id"],
            "object_uuid": e.get("dlc:objectUuid"),
            "locator": e.get("dlc:locator"),
            "produced_by": (e.get("dlc:producedBy") or {}).get("@id"),
            "consumed_by": consumers.get(e["@id"], []),
        }
        for e in entities.values()
        if e["@id"].startswith("#state-")
    ]
    return {
        "name": root.get("name"),
        "date_published": root.get("datePublished"),
        "profiles": _refs(root.get("conformsTo")),
        "fingerprint": root.get(FINGERPRINT_KEY),
        "workspace": {
            "sha256": workspace.get("sha256"),
            "size": workspace.get("contentSize"),
        },
        "activities": activities,
        "states": states,
        "fan_out": sorted(s["id"] for s in states if len(s["consumed_by"]) > 1),
    }
