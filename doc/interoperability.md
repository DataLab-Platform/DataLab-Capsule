# Interoperability report (DataLab Desktop and DataLab-Web)

This report states what the two editions exchange, what each checks and replays, and what the tests show, with their limits. It covers the format of profile 0.1 and the editions on the `feature/workspace-provenance` branches (October 2026).

## Editions and environments tested

| | DataLab Desktop | DataLab-Web |
| --- | --- | --- |
| Edition | 1.3.0 (development branch) | 0.9.0 (development branch) |
| Python | CPython 3.11.6, Windows | CPython 3.12.1 in Pyodide 0.26.4, Chromium, main thread and worker |
| Sigima, guidata | 1.3.0, 3.16.0 (from the checkouts) | 1.3.0, 3.16.0 (wheels from the checkouts) |
| NumPy, SciPy, h5py | 2.4.4, 1.16.3, 3.16.0 | 1.26.4, 1.12.0, 3.11.0 |
| DataLab-Capsule | 0.1.0.dev0 | 0.1.0.dev0 |

The library alone (validation, inspection, comparison, verification, registry client) also runs on CPython 3.9, 3.12 and 3.14 and in Pyodide; the registry on CPython.

## What is exchanged

| Item | Carried by | Checked by the recipient |
| --- | --- | --- |
| Workspace (signals, images, metadata, ROI, analysis results) | HDF5 file, included in the capsule or referenced by an address | SHA-256 and size against the manifest |
| Provenance ledger (activities, calls, parameters, input and output states, environments) | HDF5 block `/DataLab_Provenance`, described by the manifest | The manifest must describe the ledger exactly |
| Stored states | Saved objects | Fingerprint and ROI of each state (`altered` otherwise) |
| Revision link | `dlc:revisionOf` (parent fingerprint) | Part of the child's fingerprint |
| Light attachments | `attachments/` entries declared in the manifest | Size and SHA-256; never opened |
| Reviews, verification reports | Separate JSON documents | Fingerprint, target revision, activities named |

Identities are kept across editions: object UUIDs, state and activity identifiers, manifest and review fingerprints.

## Results

| Exchange | Evidence | Result |
| --- | --- | --- |
| Desktop chain reopened and replayed on the Web, and the reverse | E0 gate G3, reference files `desktop_chain.h5`, `web_chain.h5` | `not_verified` for the opaque activity, `exact` for the others, in both editions; environment `same` within an edition, `different` across editions |
| Two-input differences with X alignment, analysis results, ROI | E1.1, E1.2 reference files written by each edition and reopened by the other | Same verdicts; refusals carried with their code |
| Workspaces saved by the released DataLab 1.3.0 (no ledger) | E1.3 fixture | Legacy activities in both editions, never replayed, nothing invented |
| Capsules between two browsers, included and referenced workspace, attachments, registry | DataLab-Web E2E `capsule_exchange.spec.ts` (CI) | Integrity, supplied documents and local replay kept apart; everything fetched from a registry checked |
| **Two operators, Desktop and Web, without server and with a registry** (scenario of [scenario.md](scenario.md), 11 Oct 2026) | Local evidence `tracking/evidence/e5` | See below |
| **Families of operations** (14 Oct 2026, [decision 0002](decisions/0002-generic-contracts-and-generalized-replay.md)): one operation of each family (n-to-1, signals to image, complex FFT, seeded noise, float32 signal, statistics, image to signal profile, centroid, image convolution reading a global option, a detection that finds nothing), recorded by each edition and replayed by the other | Corpus sources `families_desktop.h5` and `families_web.h5` (written in Pyodide); DataLab `provenance_capture_unit_test.py`, DataLab-Web `provenance_families.spec.ts` (Pyodide) and `test_provenance_capture.py`; `test_cross_edition.py` | Same calls, parameters, roles, input states and contexts in both files. Every activity replayed `exact` in both directions (environment `different`), analysis results and the absence of result included; the recorded global option is applied whatever the reader's setting |
| **Sigima catalogue across numerical libraries** (14 Oct 2026) | Sigima qualification report and cross-environment replays of the calls captured from the validation tests (CPython 3.9, NumPy 1.26.4, SciPy 1.10.1 and CPython 3.11, NumPy 2.4.4, SciPy 1.16.3) | 213 of 215 operations qualified: 175 bitwise identical, 38 within their declared tolerance (FFT family, recursive filters, fits, reductions); phase spectrum not qualified (`different`) |

Two-operator scenario, without server then with a registry:

- A (Desktop) saved a four-activity chain; the CLI created the capsule with `protocol.txt` (`sha256:cb6b0d…`).
- B (Web, worker) inspected it (intact), saved the attachment (identical bytes), opened it, replayed every activity: `not_verified (unsupported_operation)` for the opaque one, `exact (ready)` for the three others. B added an activity and exported the child (`sha256:7d0293…`, revision of A's). B then wrote a review citing a report.
- A verified the child with B's report and review (valid). B's review was refused against A's own revision ("targets another capsule revision"). `diff` showed the added activity and state, a new environment and the attachment changes.
- A extracted the child workspace and replayed it on the Desktop. There were 4 real computations, all `exact`, including the activity added on the Web; the opaque activity is `not_verified`.
- With the registry: A published with the CLI and token; B retrieved A's revision (manifest checked) and published the child and its review from the Web. A listed the registry and fetched the child (manifest checked, review valid). The fetched manifest is identical to the child capsule's.

## Findings

1. **Fixed: Web workspaces holding a macro could not be opened on the Desktop development branch.** DataLab reads the macro `uid` added in its development version. When the group was missing (all Web workspaces and those saved before that version), a swallowed error left the HDF5 reader in a wrong section and the opening failed. The scenario found it; the reader now closes the section in every case, with a regression test (DataLab).
2. **Attachments do not follow revisions.** A child revision holds only the attachments its author selects; nothing copies the parent's. DataLab-Web says so at export, naming the parent's attachments not selected again, and `diff` reports a missing one explicitly ("removed attachment"). It is up to the author to attach them again when they still apply; with the command line, `create --revision-of` takes `--attach` the same way.
3. **Saving a workspace again may change its SHA-256.** HDF5 files are not guaranteed to be byte-reproducible: two exports of the same content may differ in the workspace SHA-256 and size (observed in both editions, not at every save). The comparison reports this under storage, not as a scientific change, and concludes `none_detected` rather than equality when nothing else differs: only the recorded activities and states are compared. Stored states are compared by fingerprint and are equal. The same holds when only attachment contents differ (an added, removed or changed attachment, such as a calibration certificate), since they are not interpreted.
4. **Desktop has no capsule or replay interface.** On the Desktop, capsules are handled with the `workspace-capsule` command line, and replay goes through `DLMainWindow.verify_provenance_activity` (`scripts/provenance_demo.py`). The Web has both in its interface.
5. **Opaque activities stay unverified.** In the runs of 11 October, `addition_constant` was recorded without identity and never replayed, in both editions. Since decision 0002 it keeps its identity and replays exactly; the files recorded before keep their opaque activity, which is never given an identity afterwards.

## Limits

- The phase spectrum (signal and image) is identified but not qualified for replay. Tolerances were measured between two CPython environments on x86-64; in Pyodide, the families scenario replays exactly, but the whole catalogue has not been replayed there.
- State fingerprints do not cover metadata read by a computation (e.g. fit parameters used by `evaluate_fit`). Interactive Desktop fits are not recorded.
- The registry has no accounts, quotas, rate limits or delete API; one token protects publishing. It must be deployed behind HTTPS and a reverse proxy (see the security analysis).
- Fingerprints prove integrity, not authorship: reviews name their author without proving it.
- Tested on Windows (Desktop) and Chromium (Web); the library CI also runs on Linux.
