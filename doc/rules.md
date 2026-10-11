# Identity, compatibility and restoration rules

This page states the rules that DataLab-Capsule implements for profile 0.1 (experimental). The normative description of the format is the [profile page](https://datalab-platform.com/capsule/0.1); the JSON Schemas are under `src/datalab_capsule/schemas`.

## Identity

| Thing | Identifier | Same thing when |
| --- | --- | --- |
| Object (signal or image) | `object_uuid`, the `__uuid` of the DataLab object | Same UUID. It survives save, open and exchange between Desktop and Web. |
| State (content of an object at one point) | `state_id` (UUID) | Two states have the same content when their fingerprint values and their ROI digests are equal (no ROI on both sides counts as equal). |
| Fingerprint | `scheme` + `value` (`sha256:<hex>`) | Same scheme and same value. `datalab-signal-v1` covers rows, dtype and units; `datalab-image-v1` covers data, dtype, shape, coordinates and units. Titles, display settings and other metadata are not covered. |
| Activity | `activity_id` (UUID) | Same identifier. Executions of one user command share a `command_id`. |
| Analysis result (artifact) | object UUID + metadata key + `digest` | Same key on the same object and same digest of the stored value. A later analysis that stores another value under the same key replaces it: the earlier artifact is reported `replaced`, never attributed to the later value. |
| Environment | `environment_id` = digest of the canonical environment record | Same digest (content-addressed: any change of a package version gives another environment). |
| Workspace ledger | `workspace_id` (UUID, `dlc:workspaceId`) | Same identifier; kept across saves of the same workspace. |
| Capsule revision | manifest fingerprint (`dlc:manifestFingerprint`) | Same fingerprint. Any change, including a name or a date, gives another revision. |
| Parent revision | `dlc:revisionOf` | At most one parent, named by its manifest fingerprint, which is part of the child's fingerprint. |
| Review | `review_fingerprint` | Same fingerprint. A review targets one revision by manifest fingerprint and never changes the capsule. |
| Attachment | name under `attachments/` + SHA-256 | Same name and same SHA-256. Attachments are declared in the manifest, so adding, removing or changing one gives another revision; `diff` lists it in its own `attachments` group, without interpreting the content: no activity uses an attachment, but a protocol or a calibration certificate may matter scientifically, and the reader judges. |

A state is **located** in a workspace when the saved object with the same UUID has the same kind, fingerprint and ROI as the state; the locator (`dlc:locator`) is its HDF5 path. A located state whose saved data or ROI no longer match is **altered**. A state without locator is **unavailable** (the object was deleted, changed before saving, or never saved).

## Compatibility

- **Profile.** A manifest must list `https://datalab-platform.com/capsule/0.1` and Process Run Crate 0.6 in `conformsTo`. Version 0.1 is experimental: terms may change before version 1.0, and a 1.0 reader is not required to read 0.1 capsules.
- **Versioned documents.** The ledger (`schema_version` 1), the HDF5 block (`schema_version` 1), verification reports (`report_version` 1) and reviews (`review_version` 1) are validated against their schemas. A reader refuses a version it does not know; it never guesses.
- **Fingerprint schemes.** A scheme name is never reused with another payload: a change of payload gets a new scheme name. This version computes `datalab-signal-v1` and `datalab-image-v1` only.
- **Operations.** A call names an operation contract (`id`, `contract_version`). Replaying needs the same identifier and contract version; otherwise the activity is refused with `unsupported_operation` or `unsupported_contract`. The implementation (package, version, Python name) is informative only.
- **Environments.** Differences between the recorded and the current environment are reported (`same`, `different`, `unknown`) and never block a replay.
- **DataLab versions.** A workspace saved by a DataLab without provenance is imported as `legacy_metadata` activities (see the README); a version mismatch or an unknown version is listed among the import issues, never hidden.
- **Unknown properties.** Manifest consumers may ignore RO-Crate properties they do not know; the library itself validates the manifest structure it needs and refuses inconsistent manifests (fingerprint, workspace entity, references).

## Restoration

What a reader can get back from a capsule, per activity:

| Status | Meaning | Rule |
| --- | --- | --- |
| `replayable` | The call names a qualified operation contract. | The activity may be recomputed as a separate candidate, never inserted in the workspace. It is recomputed only when it is eligible (`ready`): inputs present and unchanged since the activity (fingerprint), not altered in the file, preconditions of the contract met, recorded context (e.g. X alignment) applicable, parameters decodable. Otherwise the refusal code says why (`missing_input`, `input_changed`, `unsupported_context`, `invalid_parameters`, `unsupported_contract`). |
| `opaque` | The call names no qualified operation (not qualified yet, or a legacy record). | Never recomputed. Its results are available only as stored states. |
| `included` | Reserved by the report schema for a result restored from the stored workspace. | Not produced by the editions in this version. |
| `unavailable` | Reserved by the report schema for a result that can be neither restored nor recomputed. | Not produced by the editions in this version. |

Stored results follow the state rules above: a located, unaltered state can be read from the workspace; an altered or unavailable state cannot be used as a reference. A verification compares a candidate with its reference under the `exact` rule (same rows, dtype and shape, equal values, NaN at the same positions); with no available reference the verdict is `not_verified`. In this version, replay is qualified for `sigima.signal.normalize` (version 1) and `sigima.signal.difference` (version 1) only; every other operation is captured as opaque.

`verify_capsule` and the `verify` command never compute: they check integrity (manifest, workspace, stored states) and the consistency of supplied reports and reviews. Recomputation is a feature of the editions.
