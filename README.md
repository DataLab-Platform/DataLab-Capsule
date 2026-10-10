# DataLab-Capsule

Exchange and verify scientific workspace provenance as open RO-Crate capsules (library and CLI, no DataLab required).

Status: experimental, under development. Nothing is published yet.

## Scope

- Neutral provenance model shared by DataLab Desktop and DataLab-Web: operation calls, states, activities, environments (JSON Schemas under `src/datalab_capsule/schemas`).
- Shared replay preparation (eligibility codes), the `exact` comparison rule and verification reports.
- HDF5 provenance block (`/DataLab_Provenance`) read and written with h5py alone; links and virtual datasets are refused.
- Integrity primitives: RFC 8785 canonical JSON, SHA-256 digests, the `datalab-signal-v1` state fingerprint.
- Capsules (`.dlcapsule`): a ZIP archive holding `workspace.h5` and an RO-Crate 1.3 manifest (`ro-crate-metadata.json`) that conforms to Process Run Crate 0.6 and to the experimental [DataLab capsule profile 0.1](https://datalab-platform.com/capsule/0.1) (terms: `https://datalab-platform.com/capsule/terms#`). The reader refuses unsafe archives (path traversal, duplicates, links, encryption, size and compression-ratio limits) and never accesses the network.
- Pure Python for CPython 3.9+ and Pyodide (Python 3.12). The core only needs the standard library and `jsonschema`; HDF5 helpers use NumPy and h5py.
- The package never imports DataLab, Sigima, SigimaX or Qt, and never executes code named in a file.

## Command line

Validating and inspecting a capsule needs neither NumPy nor h5py:

```powershell
workspace-capsule validate workspace.dlcapsule
workspace-capsule inspect workspace.dlcapsule [--json]
```

Creating a capsule from a workspace saved by DataLab (needs the `hdf5` extra):

```powershell
python examples/workspace_to_capsule.py workspace.h5 workspace.dlcapsule
```

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

## License

BSD 3-Clause, see [LICENSE](LICENSE). The embedded RO-Crate 1.3 JSON-LD context (`src/datalab_capsule/contexts/ro-crate-1.3.jsonld`) is distributed by its authors under CC0 1.0.
