# Interoperability report (DataLab Desktop and DataLab-Web)

This report states what the two editions exchange, what each checks and replays, and what the tests show, with their limits. It covers the format of profile 0.1 with the released DataLab-Capsule 0.1.0, Sigima 1.3.0, guidata 3.16.0 and DataLab-Web 0.10.0, and DataLab Desktop from its development branch `develop` (not released yet, to be released as 1.4): see the [final validation](#final-validation-with-the-released-versions-20-october-2026) of 20 October 2026. The earlier runs it reports were made on the development branches, before these releases.

## Editions and environments tested

Final validation (20 October 2026), every package from PyPI except DataLab Desktop and SigimaX:

| | DataLab Desktop | DataLab-Web |
| --- | --- | --- |
| Edition | `develop` (`618c95db`; version string still 1.3.0), with SigimaX from `develop` (`4792f98`, 1.1.0 not published yet) | 0.10.0: published bundle (GitHub release, same files as <https://datalab-platform.com/web/>) and `v0.10.0` sources served in release mode |
| Python | CPython 3.12.10, Windows, new virtual environment | CPython 3.12.1 in Pyodide 0.26.4, Chromium, worker |
| Sigima, guidata | 1.3.0, 3.16.0 (PyPI) | 1.3.0, 3.16.0 (PyPI) |
| NumPy, SciPy, h5py | 2.4.6, 1.16.3, 3.16.0 | 1.26.4, 1.12.0, 3.11.0 |
| DataLab-Capsule | 0.1.0 (PyPI) | 0.1.0 (PyPI) |

The command line and the registry of operator A ran from `datalab-capsule[hdf5,registry]` 0.1.0 installed from PyPI in a separate environment holding neither DataLab, Sigima nor guidata. Earlier runs used CPython 3.11.6 and NumPy 2.4.4 for the Desktop, and development builds of the packages.

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
| **Two operators, Desktop and Web, without server and with a registry** (scenario of [scenario.md](scenario.md), 11 Oct 2026, then 17 Oct 2026 with candidate distributions) | Evidence kept with the delivery | See below |
| **Same scenario with the released versions** (20 Oct 2026) | Evidence kept with the delivery | See [final validation](#final-validation-with-the-released-versions-20-october-2026) |
| **Families of operations** (14 Oct 2026, [decision 0002](decisions/0002-generic-contracts-and-generalized-replay.md)): one operation of each family (n-to-1, signals to image, complex FFT, seeded noise, float32 signal, statistics, image to signal profile, centroid, image convolution reading a global option, a detection that finds nothing), recorded by each edition and replayed by the other | Corpus sources `families_desktop.h5` and `families_web.h5` (written in Pyodide); DataLab `provenance_capture_unit_test.py`, DataLab-Web `provenance_families.spec.ts` (Pyodide) and `test_provenance_capture.py`; `test_cross_edition.py` | Same calls, parameters, roles, input states and contexts in both files. Every activity replayed `exact` in both directions (environment `different`), analysis results and the absence of result included; the recorded global option is applied whatever the reader's setting |
| **Sigima catalogue across numerical libraries** (14 Oct 2026) | Sigima qualification report and cross-environment replays of the calls captured from the validation tests, in three environments: CPython 3.9, NumPy 1.26.4, SciPy 1.10.1; CPython 3.11, NumPy 2.4.4, SciPy 1.16.3; Pyodide 0.26.4 (WebAssembly: CPython 3.12, NumPy 1.26.4, SciPy 1.12.0), with Sigima `scripts/pyodide_replay` | 213 of 215 operations qualified: 161 must be bitwise identical, 52 have a declared tolerance (FFT family and filters, recursive filters, fits, reductions, transcendental functions, float32 data). Every call replayed in Pyodide is within its rule against both CPython environments; only the phase spectrum, not qualified, is `different` |

Costs and capacity of these paths are measured in the [performance notes](performance.md) (17 October 2026).

Two-operator scenario, without server then with a registry:

- A (Desktop) saved a four-activity chain; the CLI created the capsule with `protocol.txt` (`sha256:cb6b0d…`).
- B (Web, worker) inspected it (intact), saved the attachment (identical bytes), opened it, replayed every activity: `not_verified (unsupported_operation)` for the opaque one, `exact (ready)` for the three others. B added an activity and exported the child (`sha256:7d0293…`, revision of A's). B then wrote a review citing a report.
- A verified the child with B's report and review (valid). B's review was refused against A's own revision ("targets another capsule revision"). `diff` showed the added activity and state, a new environment and the attachment changes.
- A extracted the child workspace and replayed it on the Desktop. There were 4 real computations, all `exact`, including the activity added on the Web; the opaque activity is `not_verified`.
- With the registry: A published with the CLI and token; B retrieved A's revision (manifest checked) and published the child and its review from the Web. A listed the registry and fetched the child (manifest checked, review valid). The fetched manifest is identical to the child capsule's.

Two-operator scenario, again on 17 October 2026, with the candidate distributions (DataLab-Capsule `57989f3`, DataLab `30fa1d01`, DataLab-Web `0978a95`):

- A (Desktop, `provenance_demo.py` in an empty folder) built and saved the chain: 4 real computations, all `exact` (the addition of a constant now keeps its identity). The CLI of the candidate wheel, installed in a clean environment, created the capsule with `protocol.txt` (`sha256:aad19d…`) and published it to a local registry, refused without the token, accepted with it.
- B (Web, worker, through the dialog, `two_operator_scenario.spec.ts`) inspected, opened and replayed the four activities, all `exact (ready)`, retrieved A's revision from the registry, added an activity, exported the child (`sha256:02774a…`, revision of A's), wrote a review citing a report and published the child and the review.
- A verified the child with B's report and review (valid); B's review was refused against A's revision; `diff` showed the added activity and state as the only scientific changes; the child fetched from the registry was checked (review valid) and its manifest is identical to the child capsule's.
- A replayed the child on the Desktop: 5 real computations, all `exact`, including the activity added on the Web.
- The production bundle of DataLab-Web, extracted from its tarball and served by a static server with the candidate wheels, inspected, opened and replayed a capsule exported by the candidate DataLab Desktop, and verified a step from the Provenance tab (`release_build.spec.ts`, candidate level). The candidate DataLab Desktop, installed in a clean environment and built with PyInstaller, exported, inspected and reopened a capsule and kept its revision after a restart (`capsule_smoke_check.py`, frozen probe).

### Final validation with the released versions (20 October 2026)

The scenario was run again with the published packages only (environments in the first table): DataLab-Capsule 0.1.0, Sigima 1.3.0 and guidata 3.16.0 from PyPI, DataLab-Web 0.10.0, and DataLab Desktop from `develop`, which is not released yet.

- **Operator A, DataLab Desktop.** `provenance_demo.py`, run from a new environment, built and saved the chain: four activities (addition of a constant, then normalizations, with a fan-out). After reopening the file, there were 4 real computations, all `exact`. The published command line created the capsule with `protocol.txt` (`sha256:27065e…`, 4 activities, 5 states, 1 attachment). It then published the capsule to the published registry: refused without the token (401), accepted with it. The manifest and the attachment were sent, never the workspace.
- **Operator B, DataLab-Web 0.10.0.** The `v0.10.0` sources were served in release mode, which installs only the published pins. A probe recorded sigima 1.3.0, guidata 3.16.0 and datalab-capsule 0.1.0 downloaded from `files.pythonhosted.org`. `two_operator_scenario.spec.ts` (worker, through the dialog) ran the following steps:
  - The capsule was intact, and the attachment was saved with identical bytes.
  - The four activities were replayed `exact (ready)`: recorded with NumPy 2.4.6 on CPython, replayed with NumPy 1.26.4 in WebAssembly.
  - B normalized `S0` (`amplitude`) and exported the child (`sha256:ba0390…`, revision of A's), then wrote a review citing a report.
  - B retrieved A's revision from the registry (manifest checked), then published the child and its review.
- **Operator A, checks.** `verify` of the child with B's report and review: workspace verified, 6 stored states, none altered, report and review valid. B's review was refused against A's revision. `diff` reported the revision link and two scientific changes only (added activity and state). It listed apart the presentation, storage (locators, workspace SHA-256 and size), environment (one added) and attachment changes (`protocol.txt` removed, see finding 2).
- **Operator A, Desktop replay of B's workspace.** There were 5 real computations, all `exact`. The environment was `same` for the activities recorded on the Desktop and `different` for the one recorded in the browser, which did not prevent its replay.
- **Registry.** `list` showed both revisions with the parent link. `fetch` checked the manifest and the review (valid), and the fetched manifest is identical to the child capsule's. The registry folder held two manifests, one attachment and one review, no workspace. After the registry was stopped, both capsules still verified.
- **Published Web bundle.** The `datalab-web-0.10.0.tgz` of the GitHub release was served by a static server; its `index.html` and runtime entry points are byte-identical to the deployed site. A's capsule was intact, opened and replayed (`exact`), and a step was verified from the Provenance tab (`release_build.spec.ts`). The sigima, guidata and datalab-capsule wheels came from PyPI only.
- **Public corpus with the published command line.** The 14 valid capsules pass `validate` and `verify`; the 12 invalid capsules are refused by `verify`.
- **Published profile.** The schemas and the JSON-LD context served under <https://datalab-platform.com/capsule/0.1/> are byte-identical to those of the datalab-capsule 0.1.0 distribution.

These runs drive the interfaces through scripts and Playwright; [scenario.md](scenario.md) gives the same steps for operators working by hand. The evidence of each run (files, SHA-256 of the distributions and of every file produced) is kept with the delivery, not in this repository.

## Findings

1. **Fixed: Web workspaces holding a macro could not be opened on the Desktop development branch.** DataLab reads the macro `uid` added in its development version. When the group was missing (all Web workspaces and those saved before that version), a swallowed error left the HDF5 reader in a wrong section and the opening failed. The scenario found it; the reader now closes the section in every case, with a regression test (DataLab).
2. **Attachments do not follow revisions.** A child revision holds only the attachments its author selects; nothing copies the parent's. DataLab-Web says so at export, naming the parent's attachments not selected again, and `diff` reports a missing one explicitly ("removed attachment"). It is up to the author to attach them again when they still apply; with the command line, `create --revision-of` takes `--attach` the same way.
3. **Saving a workspace again may change its SHA-256.** HDF5 files are not guaranteed to be byte-reproducible: two exports of the same content may differ in the workspace SHA-256 and size (observed in both editions, not at every save). The comparison reports this under storage, not as a scientific change, and concludes `none_detected` rather than equality when nothing else differs: only the recorded activities and states are compared. Stored states are compared by fingerprint and are equal. The same holds when only attachment contents differ (an added, removed or changed attachment, such as a calibration certificate), since they are not interpreted.
4. **Addressed: Desktop had no capsule or replay interface.** At the time of this scenario, capsules were handled on the Desktop with the `workspace-capsule` command line, and replay went through `DLMainWindow.verify_provenance_activity` (`scripts/provenance_demo.py`), while the Web had both in its interface. DataLab Desktop now has the same _File > Workspace capsule…_ dialog as DataLab-Web (export, inspect, local replay, revisions, registry), built on `datalab_capsule.exchange` so that both editions give the same answers.
5. **Opaque activities stay unverified.** In the runs of 11 October, `addition_constant` was recorded without identity and never replayed, in both editions. Since decision 0002 it keeps its identity and replays exactly; the files recorded before keep their opaque activity, which is never given an identity afterwards.
6. **Fixed: an out-of-range constant added to an integer image depended on the platform.** Replaying the catalogue in Pyodide showed that Sigima converted the constant to the image type first: adding -1 to a `uint8` image added 255 on x86-64 and nothing in WebAssembly. Sigima now truncates the constant toward zero without converting it (adding -1 subtracts 1); its validation test, which derived its expected values with the same conversion, now uses an independent oracle.
7. **Last-bit differences in WebAssembly.** The platform math library (logarithms, trigonometric functions, exponentials) and FFT-based filters differ from x86-64 in the last bits; 14 operations declared exact now declare a measured tolerance, still exact within the same environment. The enclosing circle has an integer center, which such a difference may move by one pixel (tolerance of one pixel).

## Limits

### Capture paths

The qualified-operation count describes contracts, not every GUI/API path. The following scope is checked against the Desktop processors and History replay, and the Web bootstrap and macro proxy:

| Family | Captured execution or limit |
| --- | --- |
| Ordinary/grouped 1-to-1, 2-to-1, n-to-1 and 1-to-n processing | Processor paths record the input states and committed outputs; known contracts identify the calls, otherwise they can remain opaque |
| Analyses, including a successful detection with no result | Artifact outputs or explicit absence of a result are recorded; detection-created ROI do not invalidate an otherwise unchanged input |
| Signal/image conversions and ROI extraction | Recorded through the processing paths, including cross-panel outputs |
| Processing/Recompute and replay of supported History compute actions | New executions are recorded; local verification alone does not insert or record a result |
| Import, creation, direct data/ROI/metadata edits | Not scientific calls describing acquisition or arbitrary mutations; states may be observed without known production when later used as inputs or explicitly observed |
| Interactive Desktop fitting and markers-table-to-signal | Not currently recorded scientific calls, even where the History keeps an editable action |
| Plugins, macros, notebooks, scripts and remote APIs | Supported processor calls are captured; direct object access can bypass them and functions without contracts can remain opaque |
| Failed/cancelled processing or discarded preview | Not successful executions; an adopted preview is recorded when its result is committed |

These are capture boundaries, not a ban on replay of every plugin or a claim that every script is traced. The Desktop History Record switch does not control provenance. A capture failure is logged and counted without aborting the scientific result; see the [common rules](rules.md#workspace-and-capture-boundaries).

### Replay and exchange

- The phase spectrum (signal and image) is identified but not qualified for replay. Tolerances are measured on the calls of the Sigima validation tests, in the three environments above.
- Metadata read by a computation are outside state fingerprints. They are checked only when the operation contract declares them (`reads_metadata`, e.g. the fit parameters used by `evaluate_fit`): their digest is recorded in the activity context and a change refuses the replay (`input_changed`). Interactive Desktop fits are not recorded.
- The registry has no accounts, quotas, rate limits or delete API; one token protects publishing. It must be deployed behind HTTPS and a reverse proxy (see the security analysis).
- Fingerprints prove integrity, not authorship: reviews name their author without proving it.
- Measured sizes (see [performance](performance.md)): DataLab-Web was tested up to a 233 MiB capsule (limit 256 MiB), DataLab Desktop up to 32 MiB through its dialog and the library up to 234 MiB; a ledger of 1000 activities stays usable in both editions. DataLab-Web needs network access to start (no offline mode).
- Tested on Windows (Desktop) and Chromium (Web); the library CI also runs on Linux.
- Numerical arrays of earlier states are not all retained. ROI-only restoration on copies requires unchanged input data, and never repairs missing or altered output references.
- Environment descriptions are not installers. Physical acquisition, conversions before native objects, and arbitrary external dependencies are not automatically reconstructed. RO-Crate/PROV compatibility is not a guarantee that every third-party reader restores or replays DataLab; profile 0.1 remains experimental.
