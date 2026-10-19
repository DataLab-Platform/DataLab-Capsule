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
- **Execution context.** The context of an activity records what the computation depends on besides inputs and parameters: the X-alignment policy applied (identifier, version and outcome), the global options read by the computation (e.g. `auto_normalize_kernel`) and the digests of the metadata of inputs it reads (e.g. the fit parameters read by `evaluate_fit`; state fingerprints do not cover metadata). A replay applies the recorded context; an unknown policy, a missing option or unrecorded metadata is refused (`unsupported_context`), and metadata read that changed since the activity give `input_changed`.
- **Environments.** Differences between the recorded and the current environment are reported (`same`, `different`, `unknown`) and never block a replay.
- **DataLab versions.** A workspace saved by a DataLab without provenance is imported as `legacy_metadata` activities (see the README); a version mismatch or an unknown version is listed among the import issues, never hidden.
- **Unknown properties.** Manifest consumers may ignore RO-Crate properties they do not know; the library itself validates the manifest structure it needs and refuses inconsistent manifests (fingerprint, workspace entity, references).

## Restoration

What a reader can get back from a capsule, per activity:

| Status | Meaning | Rule |
| --- | --- | --- |
| `replayable` | The call names an operation contract (identifier and version). | The activity may be recomputed as a separate candidate, never inserted in the workspace, on copies of its inputs. It is recomputed only when it is eligible (`ready`): local contract qualified for that version, inputs present and unchanged since the activity (fingerprint and metadata read by the computation; an input whose regions of interest alone changed, e.g. regions added by a detection, is replayed on a copy carrying the recorded ones), not altered in the file, recorded context (X alignment, global options) applicable, parameters decodable. Otherwise the refusal code says why (`missing_input`, `input_changed`, `unsupported_context`, `invalid_parameters`, `unsupported_contract`), with a reason. |
| `opaque` | The call names no operation: a function unknown to the computation library (e.g. a plugin), a legacy record, or an activity recorded without identity by an earlier version. | Never recomputed (`unsupported_operation`). Its results are available only as stored states. |
| `included` | Reserved by the report schema for a result restored from the stored workspace. | Not produced by the editions in this version. |
| `unavailable` | Reserved by the report schema for a result that can be neither restored nor recomputed. | Not produced by the editions in this version. |

Stored results follow the state rules above: a located, unaltered state can be read from the workspace; an altered or unavailable state cannot be used as a reference. A verification compares every output of the candidate with the recorded one, by role, under the comparison rule declared by the qualification of the operation:

- `exact` (the default): same rows, dtype and shape, equal values, NaN at the same positions (for complex values, in both parts);
- a tolerance, declared with its justification for computations whose last bits depend on the numerical libraries (Fourier transforms, recursive filters, fits, reductions): values within `atol + rtol * |reference|` give `within_tolerance`, with the observed errors; the rule states whether results must still be identical within an identical environment, which is then applied exactly.

Signals and images are compared on their rows, regions of interest and units; analysis results (tables, geometries) on their values under the rule and on their structure (kinds, column names, ROI indices) exactly; an optional output recorded without result (e.g. a detection that found nothing) must have no result again. A missing, altered or replaced reference gives `not_verified` for that output; the verdict of the report is the most severe verdict of its outputs. A rule is never widened to hide a difference: `different` is reported. The phase spectrum (signal and image) is identified but not qualified, and its replay is refused with `unsupported_contract`; dated qualification counts and environments belong in the [interoperability report](interoperability.md).

`verify_capsule` and the `verify` command never compute: they check integrity (manifest, workspace, stored states) and the consistency of supplied reports and reviews. Recomputation is a feature of the editions.

Without h5py, workspace checking covers SHA-256 and size only (`integrity_only`), not individual HDF5 states. A referenced workspace that was not supplied is `not_available`; its address is not fetched automatically. A supplied report remains its author's assertion even when its structure and target are valid.

## Workspace and capture boundaries

A capsule describes the workspace exported by its author, its ledger and chosen attachments. It does not select the minimal analysis for a result or collect every external resource, source CSV/TIFF file, instrument or acquisition step. Copying a result alone into another workspace does not reconstruct its necessary inputs. A smaller verifiable exchange requires deliberately preserving or rebuilding useful inputs and checking the remaining references, not manually editing the ledger.

The ledger keeps descriptions, fingerprints and dependencies, not snapshots of every earlier array or analysis value. Removing an object does not remove earlier activities. A later analysis under the same metadata key can leave an earlier artifact `replaced`. ROI-only input restoration is limited to copies with unchanged numerical data; it neither reconstructs missing arrays nor makes an absent, changed or replaced output a valid reference.

Appending workspace data does not merge ledgers or change the current capsule parent. Objects taking over an earlier UUID get an identity break: states before that break belong to another object, including analysis references. Replacing a workspace restores its ledger and origin. Generic HDF5 browsing imports datasets, not the scientific production of a native workspace.

Automatic capture applies to the [supported paths](interoperability.md#capture-paths), independently of the Desktop History Record mode. The History is an editable recipe, not the append-only execution record. Direct object creation/edits can bypass capture, and calls without an operation contract can remain opaque. A recording failure does not abort scientific processing: `Recorder.capture_failures` counts it and the `datalab_capsule.recorder` logger reports it. The result can therefore exist without an activity; no user-interface completeness indicator is implied.

Cancelled/failed processing and discarded previews are not successful executions. A detection that succeeds without a result is recorded and compared as such. Legacy records have unknown input states and possibly partial parameters/environments; their names never acquire executable meaning retroactively.

## Verification and revision boundaries

An explicit local replay verifies one activity on copies. It does not rebuild a whole chain, recover all historical states or implement a workflow for new data. Reapplying a processing to current inputs is a new execution. Environment differences are reported, not automatically installed or repaired; successful exact/tolerant comparison is neither universal reproducibility, scientific validity nor authenticated authorship.

Only dependencies declared by the contract are captured and reapplied. State fingerprints exclude arbitrary metadata; metadata read by an operation are checked through their recorded digests. For example, altered fit parameters used by `evaluate_fit` may give `input_changed` while the signal arrays still match. A missing declaration cannot be inferred from the archive.

`none_detected` compares the recorded activities and states, not every unrecorded aspect of an analysis. HDF5 storage bytes, publication dates or presentation can change capsule identity without a new scientific calculation. Attachments are compared as declared bytes in their own group, without scientific interpretation: a changed protocol can matter even when no scientific difference is detected.

Revisions are immutable, with one direct parent at most. The editions propose the capsule whose workspace was opened as parent, also after saving/reopening HDF5: opening A and exporting B does not move the next proposed parent from A to B. Inspection alone does not change that origin. There is no automatic merge, concurrent collaboration, conflict resolver or complete review approval process. Local revision lists keep at most 100 manifests each, not the capsule files; browser storage deletion removes the Web list.

The workspace licence is independent of the software licence. The manifest contains the supplied declaration, or the neutral default "No licence was specified for this workspace." No BSD licence is assigned to data by default.

## Bounded views and resources

Consultation reads the ledger without arrays or calculations. Web disk-mode data checks can report nonresident arrays without loading them; explicit replay can load its required objects, except opaque calls which are refused before data access.

The per-result projection limits input entries to 200 per activity, analyses to 50 and states to 20 per object, with expansion depth 64. Its `*_truncated` fields and `depth_limit` report omitted view content, not removed ledger records. Edition formatters abbreviate values to 120 characters and collections to 8 items; consult the record for details.

JSON volume and structural limits also bound the ledger even without a fixed maximum activity count. [Resource policies and measurements](performance.md) distinguish accepted size from tested capacity. [Trust and privacy limits](security.md) apply to all exchanges: profile 0.1 is experimental, not a universal sandbox or a promise that any RO-Crate/PROV reader can restore DataLab.
