# DataLab-Capsule

Exchange and verify scientific workspace provenance as open RO-Crate capsules (library and CLI, no DataLab required).

Status: experimental, under development. Nothing is published yet.

## Scope

- Neutral provenance model shared by DataLab Desktop and DataLab-Web: operation calls, states, activities, environments (JSON Schemas under `src/datalab_capsule/schemas`).
- Shared replay preparation (eligibility codes), the `exact` comparison rule and verification reports.
- HDF5 provenance block (`/DataLab_Provenance`) read and written with h5py alone; links and virtual datasets are refused.
- Integrity primitives: RFC 8785 canonical JSON, SHA-256 digests, the `datalab-signal-v1` and `datalab-image-v1` state fingerprints (data, coordinates or calibration, units). A state also records the digest of its ROI definition, and an analysis result the digest of its stored value: a saved object whose data, calibration, units or ROI differ from its state is `altered`, and a result replaced by a later analysis is never attributed to the earlier one. Fingerprints are streamed into SHA-256 without copying the arrays.
- Legacy workspaces: processing records stored by DataLab in object metadata (`__processing_parameters`, `__analysis_parameters`) of objects unknown to the ledger become `legacy_metadata` activities. They keep the record as stored and invent nothing: inputs are bound to the recorded objects (state unknown), the call is opaque without parameters, environment, edition and times are null, and import issues (missing ancestry, unavailable inputs, unsupported operations, version warnings...) are listed. They claim no result: the state of the holding object observed when the workspace was opened is named separately (`observed_state_id`), as an observed state that no activity produced. They are never replayed.
- Capsules (`.dlcapsule`): a ZIP archive holding `workspace.h5` and an RO-Crate 1.3 manifest (`ro-crate-metadata.json`) that conforms to Process Run Crate 0.6 and to the experimental [DataLab capsule profile 0.1](https://datalab-platform.com/capsule/0.1) (terms: `https://datalab-platform.com/capsule/terms#`). The reader refuses unsafe archives (path traversal, duplicates, links, encryption, compressed entries, size limits) and never accesses the network. It holds the whole archive in memory: it checks the archive size before reading it, then the size of each entry. Each edition passes its own `SizePolicy`; the default one (4 GiB workspace) is an accepted upper bound, not a tested capacity.
- Revisions and reviews: a capsule may name one parent revision by its manifest fingerprint (`dlc:revisionOf`), which is part of its own fingerprint. A review is a separate JSON document (`review-1` schema) that targets one revision by fingerprint, states who reviewed it and what was found (`endorsed`, `disputed` or `comment`), may cite verification reports by fingerprint, and has its own fingerprint. A review never modifies the capsule.
- Referenced workspaces: a capsule may reference its workspace by an absolute IRI instead of including `workspace.h5` (`dlc:referenced`). The library never fetches it: the caller passes an explicit resolver, and the bytes obtained must match the SHA-256 and size recorded in the manifest.
- Light attachments: up to 8 files of at most 16 MiB each, chosen by the author (protocol, notes, small tables...), stored under `attachments/` and declared in the manifest (`dlc:attachment`) with their media type, size and SHA-256. They are part of the manifest fingerprint. The reader refuses undeclared, missing or altered attachments. They are returned as bytes, written to disk only on explicit request, and never opened or executed.
- Semantic diff (`diff_manifests`): changes between two capsules are sorted into scientific (activities, parameters, states, fingerprints, ROI, workspace content), presentation (name, description, licence, date, locators, workspace storage), environment (environments, packages, the environment of each activity) and attachments (added, removed or changed; their content is not interpreted, since a protocol or a calibration certificate may matter scientifically).
- Verification without computation (`verify_capsule`): the archive and manifest, the workspace integrity, the match between the manifest and the workspace ledger, the fingerprints and ROI of every stored state (with h5py), and supplied verification reports and reviews. Recomputing an activity is the replay of an edition, never this package's job.
- Pure Python for CPython 3.9+ and Pyodide (Python 3.12). The core only needs the standard library and `jsonschema`; HDF5 helpers use NumPy and h5py.
- The package never imports DataLab, Sigima, SigimaX or Qt, and never executes code named in a file.

## Command line

Validating, inspecting and comparing capsules needs neither NumPy nor h5py:

```powershell
workspace-capsule validate workspace.dlcapsule
workspace-capsule inspect workspace.dlcapsule [--json]
workspace-capsule diff old.dlcapsule new.dlcapsule [--json]
```

`diff` exits with 0 when the capsules are identical and 1 when they differ.

Writing one attachment to a new file (an existing file is never overwritten, and the attachment is never opened):

```powershell
workspace-capsule attachment workspace.dlcapsule protocol.txt protocol.txt
```

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

## Documentation

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
node run_pyodide.mjs ..\..\dist\datalab_capsule-0.1.0.dev0-py3-none-any.whl
```

Cost of image fingerprints (time and peak memory), in CPython and in Pyodide:

```powershell
.\.venv\Scripts\python.exe benchmarks\fingerprint_cost.py
cd tests\pyodide
node run_benchmark.mjs ..\..\dist\datalab_capsule-0.1.0.dev0-py3-none-any.whl
```

## License

BSD 3-Clause, see [LICENSE](LICENSE). The embedded RO-Crate 1.3 JSON-LD context (`src/datalab_capsule/contexts/ro-crate-1.3.jsonld`) is distributed by its authors under CC0 1.0.
