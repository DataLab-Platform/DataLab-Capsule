# DataLab-Capsule AI Coding Agent Instructions

This document provides repository-local guidance for AI coding agents working on **DataLab-Capsule**.

## Project Overview

**DataLab-Capsule** (`datalab-capsule`) is the shared provenance and capsule library for DataLab Desktop and DataLab-Web. It allows exchanging and verifying scientific workspace provenance as open RO-Crate capsules (`.dlcapsule`), both as a Python library and via the `workspace-capsule` / `workspace-capsule-registry` command-line tools, without requiring DataLab, Sigima, SigimaX, or Qt.

### Technology Stack

- **Python**: 3.10+ (`requires-python = ">=3.10, <4"`, CPython 3.10–3.14 and Pyodide)
- **Core**: `jsonschema` (≥ 4.17) + Python standard library only
- **Optional (`hdf5` extra)**: `NumPy` (≥ 1.22), `h5py` (≥ 3.6)
- **Optional (`registry` extra)**: `fastapi` (≥ 0.110), `uvicorn` (≥ 0.27)
- **Optional (`test` extra)**: `pytest` (≥ 7), `NumPy` (≥ 1.22), `h5py` (≥ 3.6), `httpx` (≥ 0.27)
- **Linting/Formatting**: Ruff (`line-length = 88`, `target-version = "py310"`, Google docstrings)

### Architecture

```
DataLab-Capsule/
├── src/datalab_capsule/
│   ├── __init__.py          # Package version (__version__) and public exports
│   ├── archive.py           # Safe ZIP capsule reader/writer (.dlcapsule)
│   ├── calls.py             # Operation call descriptions
│   ├── cli.py               # workspace-capsule and workspace-capsule-registry CLIs
│   ├── compare.py           # Result comparison and tolerance rules
│   ├── diff.py              # Semantic manifest diffing (diff_manifests)
│   ├── environment.py       # Execution environment descriptors
│   ├── exchange.py          # Shared high-level capsule interface for Desktop & Web
│   ├── hdf5.py              # /DataLab_Provenance HDF5 block reader/writer
│   ├── inspection.py        # Provenance inspection without computation
│   ├── integrity.py         # RFC 8785 canonical JSON, SHA-256, state fingerprints
│   ├── ledger.py            # Provenance ledger model (states, activities, calls)
│   ├── legacy.py            # Legacy DataLab metadata import
│   ├── manifest.py          # RO-Crate 1.3 / Process Run Crate 0.6 manifest generator & validator
│   ├── recorder.py          # Activity recording
│   ├── registry.py          # Optional manifest/attachment/review registry store (SQLite + files)
│   ├── registry_app.py      # FastAPI registry application
│   ├── registry_client.py   # Registry HTTP client with integrity checks
│   ├── replay.py            # Replay preparation and eligibility checks
│   ├── review.py            # Capsule revision reviews
│   ├── verify.py            # Offline capsule verification (verify_capsule)
│   ├── schemas/             # JSON Schemas
│   └── contexts/            # Embedded RO-Crate 1.3 JSON-LD context
├── tests/                   # pytest test suite, fixture corpus, Pyodide runner
├── benchmarks/              # Fingerprint and capsule cost benchmarks
├── doc/                     # Markdown documentation and ADRs
├── CHANGELOG.md             # Release notes (Keep a Changelog format)
└── pyproject.toml           # Project configuration and dependencies
```

**Strict Isolation Rule**: `datalab_capsule` must **never** import `datalab`, `sigima`, `sigimax`, `guidata`, `plotpy`, or `qtpy` (`tests/test_no_app_imports.py`), and must never execute code named in a file.

## Development Workflows

### Running Commands

Use the local virtual environment (`.venv`) to run tests and linters:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check .
```

### Changing Schemas or Manifest Format

If a change affects manifests or schemas:
1. Update generator and validator together in `manifest.py`, then regenerate/check the corpus (`python -m tests.corpus`) and `tests/fixtures/corpus/expected.json`.
2. Verify golden vectors in CPython and Pyodide (`tests/pyodide/run_pyodide.mjs`).
3. Check distribution and profile resources with `scripts/check_dist.py` and `scripts/check_profile.py`.

## Release Notes Guidelines (`CHANGELOG.md`)

**Location**: `CHANGELOG.md` (follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and Semantic Versioning; the release workflow extracts `## [X.Y.Z]` sections automatically).

### Dependency Version Tracking (`Requirements` section)

Every new release entry (`## [X.Y.Z] - YYYY-MM-DD`) in `CHANGELOG.md` must include a dedicated **`### Requirements`** section tracking minimum version updates from `pyproject.toml` (`requires-python`, `[project.dependencies]`, and `[project.optional-dependencies]`).

Distinguish three categories of dependencies (include only categories relevant to the release / project):

1. **DataLab-Platform** (`datalab-platform` ecosystem packages): none (`datalab-capsule` is a foundational library and never depends on `Sigima`, `SigimaX`, or `DataLab`, though downstream `DataLab-Platform` compatibility changes may be noted when relevant)
2. **PlotPyStack**: none (`datalab-capsule` never depends on `guidata`, `PlotPy`, or `PythonQwt`)
3. **Third-party** (and Python runtime):
   - Core & runtime: `Python` (`requires-python >= 3.10`), `jsonschema` (≥ 4.17)
   - `hdf5` extra: `NumPy` (≥ 1.22), `h5py` (≥ 3.6)
   - `registry` extra: `fastapi` (≥ 0.110), `uvicorn` (≥ 0.27)
   - `test` extra: `pytest` (≥ 7), `httpx` (≥ 0.27)

**Rules**:
- Whenever a minimum required version is bumped or a dependency is added/updated in `pyproject.toml`, document it in the release's `### Requirements` section in `CHANGELOG.md`, grouped/distinguished according to these three categories, and state the user-visible impact.

**Example**:
```markdown
## [0.2.0] - 2026-11-05

### Requirements

- **Third-party**:
  - Python ≥ 3.10 is now required (`requires-python = ">=3.10, <4"`). Python 3.9 users must keep an earlier compatible release; the Pyodide runtime is unchanged.
```
