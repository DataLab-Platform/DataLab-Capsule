# API

DataLab-Capsule is a pure Python library (CPython 3.9+ and Pyodide). The core needs the standard library and `jsonschema`; functions marked **h5py** also need NumPy and h5py. Every error derives from `datalab_capsule.integrity.ProvenanceError`. Nothing in the library accesses the network or executes code named in a file.

## Read, verify and compare capsules

```python
from datalab_capsule.archive import read_capsule
from datalab_capsule.diff import diff_manifests
from datalab_capsule.manifest import inspect_manifest
from datalab_capsule.verify import verify_capsule

capsule = read_capsule("workspace.dlcapsule")  # safe reader, validated manifest
summary = inspect_manifest(capsule.manifest)  # activities, roles, states, workspace
result = verify_capsule("workspace.dlcapsule")  # integrity checks, no computation
changes = diff_manifests(old.manifest, new.manifest)
```

| Function | Purpose |
| --- | --- |
| `archive.read_capsule(source, policy=DEFAULT_SIZE_POLICY, resolve_workspace=None)` | Read a capsule (path or bytes) in memory and check it: archive safety, size limits, manifest validity and fingerprint, workspace SHA-256 and size. Returns `Capsule(manifest, workspace, workspace_ref, attachments)`; `workspace` is None for a referenced workspace without resolver; `attachments` maps each declared attachment name to its checked bytes. `resolve_workspace(iri) -> bytes` is the only way a referenced workspace is obtained. Raises `CapsuleError` or `ManifestError`. |
| `archive.SizePolicy(max_archive_bytes, max_manifest_bytes, max_workspace_bytes, max_attachment_bytes)` | Size limits of the calling edition. |
| `manifest.validate_manifest(manifest)` | Offline validation (no JSON-LD processor): structure, terms defined by the embedded RO-Crate context or the profile, references, fingerprint, revision, embedded JSON. Raises `ManifestError` listing every problem. |
| `manifest.inspect_manifest(manifest)` | Summary: name, description, licence, date, profiles, fingerprint, parent revision, workspace (id, referenced, sha256, size), attachments (name, media type, size, sha256, description), activities (roles in order, analysis-result digests, parameters, origin, limits, legacy record, observed state), states (kind, object, fingerprint, locator, ROI, producer, consumers). |
| `manifest.manifest_fingerprint(manifest)` | `sha256:` digest of the RFC 8785 canonical JSON of the manifest without its own fingerprint. |
| `verify.verify_capsule(source, *, resolve_workspace=None, reports=(), reviews=(), policy=...)` | **h5py** for the stored states. Checks the capsule, then the workspace ledger against the manifest and every stored state (fingerprint and ROI), then the supplied reports and reviews. Returns `valid`, `fingerprint`, `workspace` (`verified`, `integrity_only` without h5py, `not_available`), `states_located`, `states_altered`, `reports`, `reviews`, `problems`. |
| `diff.diff_manifests(old, new)` | Semantic difference grouped as `scientific`, `presentation`, `environment` and `attachments` (content not interpreted), plus `identical` and `revision_of_old`. |

## Revisions and reviews

| Function | Purpose |
| --- | --- |
| `archive.create_from_hdf5(source, *, name=None, description=None, license_=None, date_published=None, revision_of=None, workspace_ref=None, attachments=())` | **h5py**. Build a capsule from a workspace holding a provenance block. `revision_of` names the parent's manifest fingerprint; `workspace_ref` (absolute IRI) makes the capsule reference its workspace instead of including it; `attachments` lists light files as `{name, data, media_type, description}` (at most `MAX_ATTACHMENTS`, each at most `MAX_ATTACHMENT_BYTES`; names: letters, digits, `.`, `_`, `-`). Returns bytes. |
| `review.create_review(target, *, reviewer, statement, verdict=None, reviewer_id=None, reports=(), date=None)` | Create a review of one revision (`target`: manifest or fingerprint). `verdict`: `endorsed`, `disputed`, `comment` or None. Cited reports keep their activity and verdict. |
| `review.validate_review(review, manifest=None)` | Check the schema, the review's own fingerprint and, when given, that it targets *manifest*. Raises `ReviewError`. |
| `review.review_fingerprint(review)` | Fingerprint of a review without its own fingerprint. |

## Workspace provenance (used by the editions)

| Function | Purpose |
| --- | --- |
| `ledger.Ledger` | Append-only ledger: `observe(object_uuid, facts)`, `record_activity(...)`, `record_legacy_activity(...)`, `states`, `activities`, `activity(id)`, `latest_state(object_uuid)`, `to_json()`, `from_json(text)`, `validate()`. |
| `calls.make_call(operation_id, contract_version, parameters, inputs)` | Build an operation call bound to states by role; `is_opaque(call)` tells whether it names no operation. |
| `environment.collect_environment(edition, edition_version, ...)`, `environment_id(record)` | Environment record (no host or user name) and its content digest. |
| `integrity.state_facts(obj)`, `signal_fingerprint(...)`, `image_fingerprint(...)`, `roi_facts(roi)`, `value_digest(value)` | State facts and fingerprints of signal-like and image-like objects (duck-typed), ROI and analysis-result digests. |
| `hdf5.save_ledger(h5file, ledger)`, `load_ledger(h5file)` | **h5py**. Write the `/DataLab_Provenance` block after the panels; read it back with the status of each state (`altered`, `unavailable`). |
| `hdf5.read_block`, `write_block`, `locate_current_states`, `locate_fingerprints`, `read_signal`, `read_image`, `read_roi`, `scan_object_index` | **h5py**. Lower-level block and object readers; links and virtual datasets are refused. |
| `legacy.import_legacy(ledger, objects, *, is_known, saved_version, current_version)`, `legacy_summary(ledger)` | Import processing records of a workspace saved without provenance as `legacy_metadata` activities, and summarise them. |
| `replay.prepare_activity(ledger, activity_id, *, resolve_contract, find_object, observe_object, check_preconditions, decode_parameters, state_status=None, apply_context=None)` | Shared eligibility checks before a replay; returns a `Plan` or a `Refusal` with its eligibility code. The edition injects every resolver. |
| `compare.compare_exact(candidate, reference)`, `compare_environments(...)`, `build_report(...)`, `validate_report(report)` | The `exact` comparison rule, environment comparison, and verification reports. |

## Canonical JSON and limits

| Name | Purpose |
| --- | --- |
| `integrity.canonical_json(value)`, `canonical_json_bytes(value)`, `json_digest(value)`, `sha256_digest(data)` | RFC 8785 canonical JSON and SHA-256 digests (`sha256:<hex>`). |
| `integrity.load_json(text)`, `check_json_depth(value)`, `MAX_JSON_DEPTH` | Parse JSON from a file with a nesting limit (64). |
| `archive.MAX_ENTRIES`, `MAX_MANIFEST_BYTES`, `MAX_WORKSPACE_BYTES`, `MAX_ATTACHMENTS`, `MAX_ATTACHMENT_BYTES`, `MAX_ARCHIVE_BYTES`, `DEFAULT_SIZE_POLICY` | Default archive limits. |
| `hdf5.MAX_SIGNAL_BYTES`, `MAX_IMAGE_BYTES`, `MAX_JSON_BYTES` | Dataset limits in workspaces. |

## Command line

`workspace-capsule validate | inspect | diff | verify | attachment` (see the README). `cli.main(argv)` runs it and returns the exit code.

## Schemas

| File | `$id` | Document |
| --- | --- | --- |
| `schemas/ledger-1.schema.json` | `urn:datalab:schema:provenance-ledger:1` | Workspace ledger |
| `schemas/report-1.schema.json` | `urn:datalab:schema:verification-report:1` | Verification report |
| `schemas/review-1.schema.json` | `urn:datalab:schema:capsule-review:1` | Review assertion |
| `contexts/ro-crate-1.3.jsonld` | (copy of `https://w3id.org/ro/crate/1.3/context`, CC0) | Pinned RO-Crate context used for offline validation |
