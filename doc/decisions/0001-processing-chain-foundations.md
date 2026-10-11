# Decision 0001: foundations of the processing chain

- Status: accepted for profile 0.1 (experimental)
- Date: 11 October 2026
- Scope: DataLab Desktop, DataLab-Web and DataLab-Capsule

## Context

DataLab records how each object was produced only in its metadata (`__processing_parameters`, `__analysis_parameters`). That record is overwritten by the next processing, binds inputs by object only, holds no state of the data, no environment, and cannot say whether the saved result still matches the processing. Desktop and Web must exchange workspaces whose processing chains can be inspected, checked and, for qualified operations, recomputed by someone else, with the same result in both editions and without running code named in a file.

## Decision

1. **A ledger of states and activities, stored in the workspace.** Each object content seen by a processing is a *state* (UUID, object UUID, content fingerprint, ROI digest). Each processing is an *activity*: an operation call whose inputs are bound by role to states, with its parameters as JSON values, its output states and analysis artifacts, its context (ROI, mask, X alignment), its environment and edition. Records are append-only. The ledger is stored as JSON in the HDF5 block `/DataLab_Provenance`, next to the objects, so a workspace stays a single file.
2. **Content fingerprints, not object names.** A state is identified by what the data are (`datalab-signal-v1`, `datalab-image-v1`: SHA-256 over a canonical descriptor and the little-endian data), so any reader can tell whether a saved object is still the result of an activity, and both editions compute the same value for the same data.
3. **Operation contracts as the only executable names.** A replayable call names an operation contract of Sigima (`id`, `contract_version`) and is resolved through the registry of the edition. Parameters are decoded by the contract. Nothing in a file is imported or executed; Python names in a file are informative only.
4. **Capture always, replay only when qualified.** Every processing is recorded. An operation without a qualified contract is recorded as opaque (implementation named, never replayed). Replay is qualified operation by operation, with exact numerical oracles; in this version, `normalize` and `difference`.
5. **Recompute as a candidate, compare, report.** A replay never modifies the workspace or the ledger: it computes a candidate, compares it with the recorded reference under a named rule (`exact`), and writes a verification report.
6. **RO-Crate for exchange.** A capsule is a ZIP holding the workspace and an RO-Crate 1.3 manifest conforming to Process Run Crate 0.6 and to the DataLab profile; it maps to W3C PROV (see the profile page). The manifest has a fingerprint; revisions name one parent; reviews are separate documents.
7. **One neutral library.** DataLab-Capsule holds the model, the schemas, the HDF5 block, the capsule format and the checks. It runs in CPython and Pyodide and never imports DataLab, Sigima or Qt; both editions use it.

## Alternatives considered

- **Extending the metadata records.** Rejected as the basis: they stay attached to one object, are overwritten, and have no state identity. They are kept as the source of `legacy_metadata` activities for old workspaces, without inventing what they lack.
- **Storing code, pickles or DataSet class names to execute.** Rejected: unsafe for exchanged files and not portable between editions.
- **W3C PROV (PROV-JSON or PROV-O) as the native store.** Rejected as the store: verbose, no packaging of the data, and no place for fingerprints or replay rules without extensions. Kept as a mapping of the manifest.
- **A workflow language (CWL, Snakemake, notebooks).** Rejected for this purpose: DataLab records what was done interactively, it does not schedule workflows. A recorded chain could be exported to a workflow language later.
- **A separate provenance file next to the workspace.** Rejected: two files drift apart. The capsule groups them for exchange only, and may reference a workspace stored elsewhere by hash.

## Limits

- Workspace ledgers are not merged: appending a workspace keeps the current ledger and gives a notice. Results are not invalidated automatically when their inputs change; the check happens when a result is verified.
- Replay is qualified for two operations; every other activity is opaque. ROI, masks and uncertainties are recorded; the qualified contracts refuse inputs with ROI or uncertainties instead of guessing.
- Fingerprints cover data, coordinates and units. Titles, display settings and other metadata are not covered.
- Fingerprints and the manifest fingerprint detect changes but do not authenticate anyone: whoever edits a capsule can recompute them. Signatures and authenticated identities are out of scope of profile 0.1; a reviewer is named, not authenticated.
- Legacy activities are never replayed and claim no result: they bind objects whose state at execution time is unknown.
- The DataLab History panel and the ledger are separate: the History is an editing tool, the ledger is the record.

## Consequences

- Any reader that follows the profile can check a capsule with `json`, `zipfile`, `hashlib` and `h5py` (see `examples/third_party_reader.py`).
- Qualifying a new operation means adding its contract in Sigima and its exact oracles; the format does not change.
- A change of fingerprint payload, ledger structure or report structure requires a new scheme or schema version.
