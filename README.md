# DataLab-Capsule

Exchange and verify scientific workspace provenance as open RO-Crate capsules (library and CLI, no DataLab required).

Status: experimental, under development. Nothing is published yet.

## Scope

- Neutral provenance model shared by DataLab Desktop and DataLab-Web: operation calls, states, activities, environments (JSON Schemas under `src/datalab_capsule/schemas`).
- Shared replay preparation (eligibility codes), the `exact` comparison rule and verification reports.
- HDF5 provenance block (`/DataLab_Provenance`) read and written with h5py alone; links and virtual datasets are refused.
- Integrity primitives: RFC 8785 canonical JSON, SHA-256 digests, the `datalab-signal-v1` state fingerprint.
- Pure Python for CPython 3.9+ and Pyodide (Python 3.12). The core only needs the standard library and `jsonschema`; HDF5 helpers use NumPy and h5py.
- The package never imports DataLab, Sigima, SigimaX or Qt, and never executes code named in a file.

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

BSD 3-Clause, see [LICENSE](LICENSE).
