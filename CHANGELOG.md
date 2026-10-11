# Changelog

All notable changes to DataLab-Capsule are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The capsule format itself is versioned by its profile (<https://datalab-platform.com/capsule/0.1>).

## [Unreleased]

First public release (0.1.0), shared by DataLab Desktop and DataLab-Web.

### Added

- **Workspace provenance model:** a neutral record of processing (operation calls with their parameters, input and output states, activities, execution environments) stored in DataLab workspaces (`/DataLab_Provenance`) and read and written with h5py alone. Signal and image states are identified by fingerprints of their data, coordinates or calibration and units, and by their regions of interest; analysis results by the digest of their stored value. A state whose saved data no longer match is reported as altered.
- **Replay preparation and verification reports:** an edition can recompute a recorded activity and compare it with the stored result (`exact`, `different`, or `not_verified` with its reason); only operations with a qualified contract are replayed, and nothing named in a file is ever executed.
- **Legacy workspaces:** processing records stored by earlier DataLab versions become legacy activities that keep the record as stored, invent nothing and are never replayed.
- **Capsules (`.dlcapsule`):** a ZIP archive with the workspace and an RO-Crate 1.3 manifest (Process Run Crate 0.6 and the DataLab capsule profile 0.1). The safe reader refuses path traversal, links, duplicates, encrypted or compressed entries and oversized archives, and never accesses the network. A capsule may reference its workspace by an address instead of including it; the bytes supplied must match the manifest.
- **Revisions and reviews:** a capsule may name its parent revision; reviews are separate documents about one revision, with their own fingerprint.
- **Light attachments:** up to 8 files of 16 MiB (protocol, notes, small tables) declared in the manifest with their size and SHA-256; checked, returned as data, never opened or executed.
- **Comparison (`diff`) and verification (`verify`):** changes between two revisions sorted into scientific, presentation, storage, environment and attachment changes, with a scientific conclusion that never claims equality beyond what the manifests record; verification of the archive, the workspace, the stored states and supplied reports and reviews, without computation.
- **Command line `workspace-capsule`:** `validate`, `inspect`, `diff`, `verify`, `attachment`, `workspace`, `create`, `review`, and `registry list | publish | fetch`.
- **Optional registry (`registry` extra):** publishes and serves manifests, their attachments and reviews (never workspaces), with checks on every document, an OpenAPI description, open reading and token-protected publishing (`workspace-capsule-registry`).
- Runs on CPython 3.9 to 3.14 and in Pyodide (registry excepted).
