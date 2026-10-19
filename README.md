# DataLab-Capsule

Exchange and verify scientific workspace provenance as open RO-Crate capsules (library and CLI, no DataLab required).

Status: experimental. Version 0.1.0 is the first public release, on PyPI as `DataLab-Capsule`; it implements the experimental [DataLab capsule profile 0.1](https://datalab-platform.com/capsule/0.1), which may still change.

## Scope

- Neutral provenance model shared by DataLab Desktop and DataLab-Web: operation calls, states, activities, environments (JSON Schemas under `src/datalab_capsule/schemas`).
- Shared replay preparation (eligibility codes), comparison rules and verification reports. A report states one of four verdicts: `exact` (same values), `within_tolerance` (values within the tolerance declared and justified for that operation), `different`, or `not_verified` (with the reason: opaque operation, missing input, unavailable reference...). A rule is never widened to hide a difference; see [rules](doc/rules.md).
- HDF5 provenance block (`/DataLab_Provenance`) read and written with h5py alone; links and virtual datasets are refused.
- Integrity primitives: RFC 8785 canonical JSON, SHA-256 digests, the `datalab-signal-v1` and `datalab-image-v1` state fingerprints (data, coordinates or calibration, units). A state also records the digest of its ROI definition, and an analysis result the digest of its stored value: a saved object whose data, calibration, units or ROI differ from its state is `altered`, and a result replaced by a later analysis is never attributed to the earlier one. Fingerprints are streamed into SHA-256 without copying the arrays.
- Legacy workspaces: processing records stored by DataLab in object metadata (`__processing_parameters`, `__analysis_parameters`) of objects unknown to the ledger become `legacy_metadata` activities. They keep the record as stored and invent nothing: inputs are bound to the recorded objects (state unknown), the call is opaque without parameters, environment, edition and times are null, and import issues (missing ancestry, unavailable inputs, unsupported operations, version warnings...) are listed. They claim no result: the state of the holding object observed when the workspace was opened is named separately (`observed_state_id`), as an observed state that no activity produced. They are never replayed.
- Capsules (`.dlcapsule`): a ZIP archive holding `workspace.h5` and an RO-Crate 1.3 manifest (`ro-crate-metadata.json`) that conforms to Process Run Crate 0.6 and to the experimental [DataLab capsule profile 0.1](https://datalab-platform.com/capsule/0.1) (terms: `https://datalab-platform.com/capsule/terms#`). The reader refuses unsafe archives (path traversal, duplicates, links, encryption, compressed entries, size limits) and never accesses the network. It holds the whole archive in memory: it checks the archive size before reading it, then the size of each entry. Each edition passes its own `SizePolicy`; the default one (4 GiB workspace) is an accepted upper bound, not a tested capacity.
- Revisions and reviews: a capsule may name one parent revision by its manifest fingerprint (`dlc:revisionOf`), which is part of its own fingerprint. A review is a separate JSON document (`review-1` schema) that targets one revision by fingerprint, states who reviewed it and what was found (`endorsed`, `disputed` or `comment`), may cite verification reports by fingerprint, and has its own fingerprint. A review never modifies the capsule.
- Referenced workspaces: a capsule may reference its workspace by an absolute IRI instead of including `workspace.h5` (`dlc:referenced`). The library never fetches it: the caller passes an explicit resolver, and the bytes obtained must match the SHA-256 and size recorded in the manifest.
- Light attachments: up to 8 files of at most 16 MiB each, chosen by the author (protocol, notes, small tables...), stored under `attachments/` and declared in the manifest (`dlc:attachment`) with their media type, size and SHA-256. They are part of the manifest fingerprint. The reader refuses undeclared, missing or altered attachments. They are returned as bytes, written to disk only on explicit request, and never opened or executed.
- Semantic diff (`diff_manifests`): changes between two capsules are sorted into scientific (activities, parameters, states, fingerprints, ROI), presentation (name, description, licence, date), storage (workspace file SHA-256 and size, included or referenced, locators: different bytes do not show a scientific change), environment (environments, packages, the environment of each activity) and attachments (added, removed or changed; their content is not interpreted, since a protocol or a calibration certificate may matter scientifically). `scientific_conclusion` says what can be concluded: `differences`, `none` (no scientific change, the same workspace file and the same attachment contents) or `none_detected` (nothing differs in the recorded activities and states, but the workspace files or the attachment contents differ, so equality is not established).
- Verification without computation (`verify_capsule`): the archive and manifest, the workspace integrity, the match between the manifest and the workspace ledger, the fingerprints and ROI of every stored state (with h5py), and supplied verification reports and reviews. Recomputing an activity is the replay of an edition, never this package's job.
- Pure Python for CPython 3.9+ and Pyodide (Python 3.12). The core only needs the standard library and `jsonschema`; HDF5 helpers use NumPy and h5py.
- The package never imports DataLab, Sigima, SigimaX or Qt, and never executes code named in a file.

## Installation

Users of DataLab Desktop and DataLab-Web need nothing more than their edition: both handle capsules from _File > Workspace capsule…_ (export, inspection, local replay, revisions, registry) and use the same functions of this package (`datalab_capsule.exchange`). The [two-operator scenario](doc/scenario.md) follows that path.

The package itself is installed in one of three ways, which must not be mixed up:

- **Released version**, for users of the command line and third-party readers: `pip install DataLab-Capsule`, with the `hdf5` extra to create capsules or check stored states and the `registry` extra to run a registry.
- **Candidate qualification**, before a release: a wheel and an sdist built from the commit to qualify (`python -m build`), checked with `scripts/check_dist.py` and `scripts/check_profile.py`, then installed in a clean environment, outside the checkout.
- **Development**: editable install of this checkout, used by the sibling checkouts of DataLab and DataLab-Web (see [developer notes](doc/development.md)).

## Command line

Validating, inspecting and comparing capsules needs neither NumPy nor h5py:

```powershell
workspace-capsule validate workspace.dlcapsule
workspace-capsule inspect workspace.dlcapsule [--json]
workspace-capsule diff old.dlcapsule new.dlcapsule [--json]
```

`diff` exits with 0 when the capsules are the same revision (same manifest fingerprint) and 1 when they differ.

Writing one attachment to a new file (an existing file is never overwritten, and the attachment is never opened):

```powershell
workspace-capsule attachment workspace.dlcapsule protocol.txt protocol.txt
```

Commands for an operator working with DataLab Desktop (`create` needs the `hdf5` extra): create a capsule from a saved workspace (optionally a revision of another capsule, with light attachments), write a review, extract the checked workspace to open it in DataLab, and exchange through a registry (only these commands use the network; everything fetched is checked):

```powershell
workspace-capsule create workspace.h5 workspace.dlcapsule [--name NAME] [--description TEXT] [--revision-of PARENT.dlcapsule] [--workspace-ref IRI] [--attach FILE]...
workspace-capsule review workspace.dlcapsule review.json --reviewer NAME --statement TEXT [--verdict endorsed|disputed|comment] [--report report.json]...
workspace-capsule workspace workspace.dlcapsule workspace.h5
workspace-capsule registry list URL [--json]
workspace-capsule registry publish URL workspace.dlcapsule [--review review.json]... --token-env VARIABLE
workspace-capsule registry fetch URL FINGERPRINT FOLDER
```

`diff` also compares manifest files (`.json`) fetched from a registry. `registry fetch` does not save an attachment whose name Windows reserves for a device (`NUL`, `CON`...). The [two-operator scenario](doc/scenario.md) puts these commands together with DataLab Desktop and DataLab-Web.

Verifying a capsule reads the stored states with h5py; without h5py, it checks only the workspace SHA-256 and size and says so (`integrity_only`). A referenced workspace is checked only when its file is given; reports and reviews are optional:

```powershell
workspace-capsule verify workspace.dlcapsule [--workspace workspace.h5] [--report report.json]... [--review review.json]... [--json]
```

`verify` exits with 0 when no problem is found and 1 otherwise. It never computes or fetches anything.

Creating a capsule from a workspace saved by DataLab (needs the `hdf5` extra):

```powershell
python examples/workspace_to_capsule.py workspace.h5 workspace.dlcapsule
```

Reading and checking a capsule without this package, with `json`, `zipfile`, `hashlib` and h5py only (a third-party example that follows the published profile):

```powershell
python examples/third_party_reader.py workspace.dlcapsule
```

## Registry (optional)

A small registry publishes and serves capsule manifests, the attachments they declare and their reviews, so that revisions can be found, compared and discussed. It never stores workspaces: a manifest names its workspace by SHA-256 and size (and by address when it is referenced). Everything keeps working without a registry. Manifests are validated and identified by their fingerprint, attachments must match their declared size and SHA-256, reviews must target a published revision. Metadata is kept in SQLite and documents in files.

```powershell
pip install "DataLab-Capsule[registry]"
$env:REGISTRY_TOKEN = "<secret>"
workspace-capsule-registry registry-data --token-env REGISTRY_TOKEN --origin https://web.example
```

Reading is open; publishing needs `Authorization: Bearer <token>` when a token is set (without one, publishing is open: local use only). The API is described at `/docs` and `/openapi.json`; see [API](doc/api.md#registry).

## Documentation

- [Two-operator scenario](doc/scenario.md): DataLab Desktop and DataLab-Web, without server and with a registry
- [Interoperability report](doc/interoperability.md): what the editions exchange, check and replay, findings and limits
- [Performance and capacity](doc/performance.md): how costs are measured, results and recommended sizes
- [Developer notes](doc/development.md) and [deployment notes](doc/deployment.md) (registry, profile pages)
- [Identity, compatibility and restoration rules](doc/rules.md)
- [API](doc/api.md)
- [Security analysis](doc/security.md)
- [Decision 0001: foundations of the processing chain](doc/decisions/0001-processing-chain-foundations.md)
- [Fixture corpus](tests/fixtures/corpus/README.md), valid and invalid, with the expected command-line results
- Profile and terms: <https://datalab-platform.com/capsule/0.1>

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,dev]"
.\.venv\Scripts\python.exe -m pytest
```

Golden vectors and the test suite in Pyodide (Node 22):

```powershell
.\.venv\Scripts\python.exe -m pip wheel . --no-deps -w dist
cd tests\pyodide
npm install
node run_pyodide.mjs (Resolve-Path ..\..\dist\datalab_capsule-*.whl)
```

Cost of fingerprints and of capsules (time and memory), in CPython and in Pyodide (see [performance](doc/performance.md)):

```powershell
.\.venv\Scripts\python.exe -m pip install -r benchmarks\requirements.txt
.\.venv\Scripts\python.exe benchmarks\fingerprint_cost.py --profile quick --output build\bench\fingerprints.json
.\.venv\Scripts\python.exe benchmarks\capsule_cost.py --profile representative --output build\bench\capsules.json
cd tests\pyodide
node run_benchmark.mjs (Resolve-Path ..\..\dist\datalab_capsule-*.whl) capsule_cost.py --profile quick --output ..\..\build\bench\capsules-pyodide.json
```

## License

BSD 3-Clause, see [LICENSE](LICENSE). The embedded RO-Crate 1.3 JSON-LD context (`src/datalab_capsule/contexts/ro-crate-1.3.jsonld`) is distributed by its authors under CC0 1.0.
