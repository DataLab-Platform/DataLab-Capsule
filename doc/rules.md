# Identity, compatibility and restoration rules

This page states the rules that DataLab-Capsule implements for profile 0.1 (experimental). The normative description of the format is the [profile page](https://datalab-platform.com/capsule/0.1); the JSON Schemas are under `src/datalab_capsule/schemas`.

## Identity

| Thing | Identifier | Same thing when |
| --- | --- | --- |
| Object (signal or image) | `object_uuid`, the `__uuid` of the DataLab object | Same UUID. It survives save, open and exchange between Desktop and Web. |
| State (content of an object at one point) | `state_id` (UUID) | Two states have the same content when their fingerprint values and their ROI digests are equal (no ROI on both sides counts as equal). |
| Fingerprint | `scheme` + `value` (`sha256:<hex>`) | Same scheme and same value. `datalab-signal-v1` covers rows, dtype and units of a signal whose rows share one dtype (e.g. float64 signals); `datalab-signal-v2` covers rows, the dtype of each row and units (e.g. float32 or complex values with a float64 X row); `datalab-image-v1` covers data (any numeric dtype, complex included), dtype, shape, coordinates and units. The scheme is chosen from the content alone, so that an object and its saved rows get the same record. Titles, display settings and other metadata are not covered. |
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
- **Fingerprint schemes.** A scheme name is never reused with another payload: a change of payload gets a new scheme name. This version computes `datalab-signal-v1`, `datalab-signal-v2` and `datalab-image-v1`; a signal recorded with `datalab-signal-v1` keeps that scheme.
- **Operations.** A call names an operation contract (`id`, `contract_version`) as soon as the computation library knows the function, whether or not the contract is qualified for replay. Replaying needs the same identifier and contract version, and a local contract qualified for that version; otherwise the activity is refused with `unsupported_operation` (unknown operation) or `unsupported_contract` (version unknown or not qualified, with the reason). The implementation (package, version, Python name) is informative only and never used to find a function.
- **Parameters.** Parameters are recorded as plain JSON by the codec of the computation library: scalar values, choices by their stable key, typed forms for values without a lossless JSON form (`{"$float": "NaN"}`, `$complex`, `$tuple`, `$dict`, `$date`, `$datetime`, `$ndarray`), lists of parameter sets (e.g. several ROIs) and, for a call with several parameter arguments, one entry per argument. They are decoded against the parameter classes of the local contract; no class is built from a name read in a document.
- **Execution context.** The context of an activity records what the computation depends on besides inputs and parameters: the X-alignment policy applied (identifier, version and outcome) and the global options read by the computation (e.g. `auto_normalize_kernel`). A replay applies the recorded context; an unknown policy or a missing option is refused (`unsupported_context`).
- **Environments.** Differences between the recorded and the current environment are reported (`same`, `different`, `unknown`) and never block a replay.
- **DataLab versions.** A workspace saved by a DataLab without provenance is imported as `legacy_metadata` activities (see the README); a version mismatch or an unknown version is listed among the import issues, never hidden.
- **Unknown properties.** Manifest consumers may ignore RO-Crate properties they do not know; the library itself validates the manifest structure it needs and refuses inconsistent manifests (fingerprint, workspace entity, references).

## Restoration

What a reader can get back from a capsule, per activity:

| Status | Meaning | Rule |
| --- | --- | --- |
| `replayable` | The call names an operation contract (identifier and version). | The activity may be recomputed as a separate candidate, never inserted in the workspace, on copies of its inputs. It is recomputed only when it is eligible (`ready`): local contract qualified for that version, inputs present and unchanged since the activity (fingerprint and regions of interest), not altered in the file, recorded context (X alignment, global options) applicable, parameters decodable. Otherwise the refusal code says why (`missing_input`, `input_changed`, `unsupported_context`, `invalid_parameters`, `unsupported_contract`), with a reason. |
| `opaque` | The call names no operation: a function unknown to the computation library (e.g. a plugin), a legacy record, or an activity recorded without identity by an earlier version. | Never recomputed (`unsupported_operation`). Its results are available only as stored states. |
| `included` | Reserved by the report schema for a result restored from the stored workspace. | Not produced by the editions in this version. |
| `unavailable` | Reserved by the report schema for a result that can be neither restored nor recomputed. | Not produced by the editions in this version. |

Stored results follow the state rules above: a located, unaltered state can be read from the workspace; an altered or unavailable state cannot be used as a reference. A verification compares every output of the candidate with the recorded one, by role, under the comparison rule declared by the qualification of the operation:

- `exact` (the default): same rows, dtype and shape, equal values, NaN at the same positions (for complex values, in both parts);
- a tolerance, declared with its justification for computations whose last bits depend on the numerical libraries (Fourier transforms, recursive filters, fits, reductions): values within `atol + rtol * |reference|` give `within_tolerance`, with the observed errors; the rule states whether results must still be identical within an identical environment, which is then applied exactly.

Signals and images are compared on their rows, regions of interest and units; analysis results (tables, geometries) on their values under the rule and on their structure (kinds, column names, ROI indices) exactly; an optional output recorded without result (e.g. a detection that found nothing) must have no result again. A missing, altered or replaced reference gives `not_verified` for that output; the verdict of the report is the most severe verdict of its outputs. A rule is never widened to hide a difference: `different` is reported. With Sigima 1.3, 213 of the 215 operations of the catalogue are qualified (175 exact, 38 with a tolerance); the phase spectrum (signal and image) is identified but not qualified, and its replay is refused with `unsupported_contract`.

`verify_capsule` and the `verify` command never compute: they check integrity (manifest, workspace, stored states) and the consistency of supplied reports and reviews. Recomputation is a feature of the editions.
