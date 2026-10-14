# Fixture corpus

Public test fixtures of the DataLab workspace capsule format ([profile 0.1](https://datalab-platform.com/capsule/0.1), experimental). Any implementation of the profile can use them; `expected.json` gives, written by hand, the expected result of the `workspace-capsule` command line on each of them. The test `tests/test_corpus.py` runs every case with the network disabled and without DataLab, in CPython and in Pyodide.

Licence: BSD 3-Clause, like the repository. The data are synthetic test data.

## Sources (`sources/`)

| File | Origin |
| --- | --- |
| `capture_desktop.h5`, `capture_web.h5` | Saved by DataLab Desktop and DataLab-Web: image filter with ROI, image centroid and signal statistics (analysis results). |
| `difference_desktop.h5`, `difference_web.h5` | Saved by DataLab Desktop and DataLab-Web: A - B and B - A on different X grids (two inputs with roles, recorded X alignment). The Web file also holds a short signal used by a refusal test. |
| `families_desktop.h5`, `families_web.h5` | Saved by DataLab Desktop and by DataLab-Web in Pyodide after the same scenario: one operation of each family (n-to-1 average, signals to image, complex FFT, seeded noise, float32 signal, statistics, image to signal profile, centroid, image convolution reading a global option, a detection that finds nothing). Every activity names its operation and is replayable; `tests/test_cross_edition.py` checks that both editions recorded the same calls, parameters, roles, input states and contexts. |
| `legacy_imported.h5` | A workspace saved by the released DataLab 1.3.0 without provenance, opened by DataLab Desktop (records imported as legacy activities) and saved again. |
| `legacy_continued.h5` | The same workspace after one more processing (`normalize`), saved by DataLab Desktop. |
| `chain_missing.h5` | `../workspaces/desktop_chain.h5` after deleting an intermediate result and rewriting the provenance block (that state becomes unavailable). Derived with h5py. |
| `chain_altered.h5` | `../workspaces/desktop_chain.h5` after changing one value of an intermediate result in the file. Derived with h5py. |
| `report_difference_exact.json` | Verification of A - B by DataLab Desktop: exact replay. |
| `report_difference_altered.json` | Verification of A - B after its saved result was changed: reference altered, not verified. |
| `report_legacy_opaque.json` | Verification of a legacy activity by DataLab Desktop: refused. |
| `report_families_convolution.json`, `report_families_centroid.json`, `report_families_detection.json` | Verifications by DataLab Desktop of activities of `families_web.h5` (recorded in Pyodide, other environment): a convolution with its recorded global option, an analysis result (geometry) and a detection without result; each report compares every output (`outputs`). |

The chains `../workspaces/desktop_chain.h5` and `../workspaces/web_chain.h5` were saved by each edition (signal transformations, replayable and opaque).

## Valid fixtures (`valid/`)

| Fixture | Covers |
| --- | --- |
| `chain-desktop.dlcapsule`, `chain-web.dlcapsule` | Signals, transformations, replayable and opaque activities, both editions. |
| `capture-desktop.dlcapsule`, `capture-web.dlcapsule` | Images, ROI, analysis results (artifacts with value digests). |
| `difference-desktop.dlcapsule`, `difference-web.dlcapsule` | Two inputs with roles, recorded X alignment. |
| `families-desktop.dlcapsule`, `families-web.dlcapsule` | One operation of each family, all replayable: lists of inputs, signal to image and image to signal, complex and float32 values (`datalab-signal-v2`), seeded noise, analysis results, global option in the context, an activity without result. |
| `legacy.dlcapsule` | Legacy activities: object bindings, unknowns, import issues, observed states. |
| `legacy-revision.dlcapsule` | A revision of `legacy.dlcapsule` with one more activity (scientific change). |
| `chain-renamed.dlcapsule` | A revision of `chain-desktop.dlcapsule` with only a new name and description (presentation change). |
| `missing-resource.dlcapsule` | A state without locator (deleted result). |
| `referenced.dlcapsule` | Referenced workspace (no `workspace.h5`); its file is `sources/difference_desktop.h5`. |
| `attachments.dlcapsule` | Two light attachments (`protocol.txt` with a description, `notes.csv`) next to the `difference_desktop` workspace (attachment changes against `difference-desktop.dlcapsule`). |
| `report-difference-exact.json`, `report-difference-altered.json`, `report-legacy-opaque.json`, `report-families-*.json` | Verification reports written by DataLab Desktop (copies of the sources). |
| `report-numeric-different.json` | A report with verdict `different` for reference `[0, 1]` and candidate `[1, 2]`. Synthetic: the values were written by hand, not computed. |
| `review-endorsed.json`, `review-comment.json` | Reviews of `difference-desktop` and `legacy`, citing reports. |

## Invalid fixtures (`invalid/`)

| Fixture | Defect | Detected by |
| --- | --- | --- |
| `tampered-manifest.dlcapsule` | Name changed after fingerprinting | `validate` |
| `workspace-mismatch.dlcapsule` | Workspace bytes changed | `validate` |
| `path-traversal.dlcapsule` | Extra entry `../evil.txt` | `validate` |
| `duplicate-entry.dlcapsule` | Manifest stored twice | `validate` |
| `missing-manifest.dlcapsule` | No manifest | `validate` |
| `compressed-entry.dlcapsule` | Deflated entries | `validate` |
| `referenced-with-workspace.dlcapsule` | `workspace.h5` present although the workspace is referenced | `validate` |
| `bad-revision.dlcapsule` | `dlc:revisionOf` is not a fingerprint | `validate` |
| `attachment-undeclared.dlcapsule` | Extra `attachments/extra.txt` absent from the manifest | `validate` |
| `attachment-altered.dlcapsule` | `attachments/protocol.txt` changed after fingerprinting | `validate` |
| `ledger-mismatch.dlcapsule` | Valid manifest that does not describe the workspace ledger (a parameter differs) | `verify` |
| `altered-state.dlcapsule` | A stored state changed after saving | `verify` |
| `review-tampered.json` | Statement changed after fingerprinting | `verify --review` |
| `review-wrong-target.json` | Targets another revision | `verify --review` |
| `report-unknown-activity.json` | Names an activity absent from the capsule | `verify --report` |
| `report-invalid.json` | Unknown verdict | `verify --report` |

## Rebuilding

Capsules, reviews and invalid documents are built deterministically from the sources: `python -m tests.corpus` (from the repository root); `test_corpus.py` checks that the committed files match a fresh build. The h5py-derived sources are written once with `python -m tests.corpus --derive` (HDF5 files are not byte-reproducible). The Desktop sources (`legacy_*.h5`, `report_*.json`) were written with `scripts/corpus_desktop_sources.py` and `scripts/corpus_families_reports.py`, run in a DataLab development environment. `families_desktop.h5` is DataLab's `provenance_desktop_families.h5` (written by its `provenance_capture_unit_test.py`), `families_web.h5` is DataLab-Web's `web_families.h5` (written by `provenance_families.spec.ts`).
