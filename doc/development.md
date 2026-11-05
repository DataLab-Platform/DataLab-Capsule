# Developer notes

DataLab-Capsule is the shared provenance layer of DataLab Desktop and DataLab-Web. It is developed with the other repositories of the stack and qualified with each of them. This page says how they fit together during development and how a change reaches each edition.

## Repositories and roles

| Repository | Role | Uses DataLab-Capsule for |
| --- | --- | --- |
| `DataLab-Capsule` | Format, integrity, comparison, verification, CLI, optional registry | — |
| `Sigima` | Computation engine; operation contracts used by replay | (no dependency) |
| `DataLab` (Desktop) | Capture, HDF5 provenance block, replay | ledger, HDF5 block, replay preparation, reports |
| `DataLab-Web` | Same in Pyodide, plus the Capsule dialog and the registry client | everything above, plus capsules, review, registry checks |
| `capsule` | Published profile 0.1, terms, schemas, context (GitHub Pages) | (documents the format) |

DataLab-Capsule never imports DataLab, Sigima, SigimaX or Qt (`tests/test_no_app_imports.py`). The core needs only `jsonschema`; `hdf5` adds NumPy and h5py, `registry` adds FastAPI and uvicorn.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,dev,registry]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff format --check .   # also formats Python blocks in Markdown
.\.venv\Scripts\python.exe -m ruff check .
```

Reinstall in editable mode after changing `[project.scripts]` or the extras: an older editable install does not get new commands (for example `workspace-capsule-registry`).

The other repositories use the checkout directly:

- DataLab: its `.env` puts `..\DataLab-Capsule\src` on `PYTHONPATH` (`scripts/run_with_env.py`).
- DataLab-Web, Python tests: same, through its `.env`.
- DataLab-Web, browser: `VITE_DATALAB_CAPSULE_INSTALL_SPEC` points to a wheel built from the checkout (`pip wheel . --no-deps -w dist`). Rebuild the wheel after each change and reload the page; the Vite dev server serves it through `/@fs/`.

## Pins and CI

- DataLab-Web pins DataLab-Capsule, Sigima and guidata in `datalab-capsule-dependency.json`, `sigima-dependency.json` and `guidata-dependency.json`. `developmentRef` is a full commit hash used by development builds and CI (`scripts/*_dependency.py prepare-pyodide` builds the wheel from that commit); `publishedRequirement` is used by release builds only (`npm run build:release`, checked by `npm run release:guard`). A change to the library reaches the Web CI only after it is pushed and the pin is moved.
- DataLab-Capsule CI runs on push: CPython 3.10, 3.12 and 3.14 (with the `registry` extra), Pyodide 0.26.4 (Node), and the CLI in a clean environment without NumPy or h5py.
- DataLab-Web CI runs on demand on feature branches: `gh workflow run tests.yml -R DataLab-Platform/web --ref <branch>`. Its end-to-end job installs the registry from the pinned wheel and requires it (`DLW_REQUIRE_REGISTRY=1`), and requires provenance (`DLW_REQUIRE_PROVENANCE=1`): those tests fail rather than skip.
- DataLab CI: `gh workflow run test_pyqt5.yml` and `test_pyqt6.yml` with `-f job_to_run=build`.

## Development, candidate and release installations

| Context | DataLab-Capsule | DataLab Desktop | DataLab-Web |
| --- | --- | --- | --- |
| Development | Editable install of this checkout | `.env` puts the sibling checkouts on `PYTHONPATH` (`scripts/run_with_env.py`) | `developmentRef` pins (CI) or `VITE_DATALAB_CAPSULE_INSTALL_SPEC` (local wheel) |
| Candidate qualification | Wheel and sdist built from the commit, `scripts/check_dist.py`, `scripts/check_profile.py --package dist/<wheel>`, clean environments outside the checkout | Clean environment with the candidate wheels of the in-house packages and the third-party packages from PyPI, `pip check`, then the checks through the interface | Wheels built from the pins (`scripts/*_dependency.py prepare-pyodide`), static production build, checks through the interface only (`window.runtime` is not exposed) |
| Release | `pip install datalab-capsule` from PyPI | `pip install datalab-platform` (or the installer) with published requirements only | `publishedRequirement` pins, `npm run release:guard`, `npm run build:release` |

A check passed in one context says nothing about another: the evidence of a delivery names the context it was obtained in. Development dependencies (sibling checkouts, `.env`, pytest for the evidence scripts) are never needed by a user of a release.

Build wheels from a clean Git checkout or on CI: the published SHA-256 of the schemas and of the context are those of files with LF line endings, which `.gitattributes` enforces only on files checked out after it was added. `scripts/check_profile.py` reports a resource that differs only by its line endings.

## Changing the format

The manifest fingerprint is the identity of a revision. A change that alters manifests of existing workspaces changes their identity:

1. Update the generator and the validator together (`manifest.py`), then the corpus (`python -m tests.corpus`) and its hand-written expectations (`tests/fixtures/corpus/expected.json`).
2. Check that the golden vectors still pass in CPython and Pyodide, or explain why they change.
3. Describe the change on the profile page and in the terms (`capsule` repository), with the SHA-256 of the changed resources, then publish after the CIs, with links pinned to the library commit. `scripts/check_profile.py` compares the resources of the package with the profile page (offline; `--online URL` checks the published pages, `--tag` the links to the implementation).
4. Keep the third-party example (`examples/third_party_reader.py`) in agreement with the library (`tests/test_third_party_example.py`).

New `dlc:` terms need no list in the library (any prefixed term is accepted), but must be defined in `terms.html`.

## Evidence and scenario

- Desktop: `scripts/provenance_demo.py OUTDIR [OTHER.h5] [--reuse]` in DataLab (needs pytest, since it reuses test helpers). It refuses a folder holding evidence of an earlier run, unless `--reuse` keeps the chain workspace already built there (e.g. to verify a child revision derived from it).
- Web: the opt-in Playwright tests `workspace provenance evidence` and `two_operator_scenario.spec.ts` (`DLW_SCENARIO_DIR`, see [scenario.md](scenario.md)).
- The Web end-to-end tests drive the development server: `window.runtime` is exposed in development mode only. A production build is checked through its interface.
