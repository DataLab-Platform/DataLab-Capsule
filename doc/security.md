# Security analysis

Scope: DataLab-Capsule 0.1.0.dev0 (profile 0.1) and its use by DataLab Desktop and DataLab-Web to read, write, verify and replay workspace provenance. Date: 11 October 2026.

## Assets and trust

- **Assets**: the user's machine and browser session, the user's workspace data, and the integrity of what a capsule claims (which data, which processing, which result).
- **Untrusted inputs**: every capsule, workspace (`.h5`), verification report and review received from someone else, and every referenced workspace obtained through a resolver.
- **Trusted code**: DataLab-Capsule, the edition running it, and the Sigima operation contracts registered in that edition.

## Threats and controls

| Threat | Control | Tested by |
| --- | --- | --- |
| **Path traversal, links, unexpected files** in the archive | Only `ro-crate-metadata.json`, `workspace.h5` and declared attachments named `attachments/<name>` (letters, digits, `.`, `_`, `-`) are accepted; absolute paths, `..`, drive letters, backslashes, duplicates, symbolic links and encrypted entries are refused. Nothing is extracted to disk. | `test_capsule.py`, `test_attachments.py`, corpus `invalid/path-traversal`, `duplicate-entry`, `attachment-undeclared` |
| **Decompression bomb** | Compressed entries are refused (profile 0.1 requires stored entries), so the extracted size is the stored size. | corpus `invalid/compressed-entry` |
| **Oversized archive or entries** (memory exhaustion) | `SizePolicy`: the archive size is checked before reading; each entry against its header, then against the bytes read. Defaults: 16 entries, 64 MiB manifest, 4 GiB workspace, 8 attachments of 16 MiB. Editions pass their own, smaller policy. | `test_capsule.py` |
| **Deeply nested JSON** (crash through recursion) | Every JSON read from a file (manifest, embedded JSON values, ledger, locators, legacy records, reports and reviews read by the CLI) is limited to 64 levels; HDF5 metadata decoding is limited in depth (64) and node count (10 000). | `test_hostile_inputs.py` |
| **Manifest that validates but breaks tools** | Validation also requires that the manifest can be summarised, which `inspect`, `diff` and `verify` rely on. | `test_hostile_inputs.py` |
| **Network access** (tracking, server-side request forgery, offline failure) | The library never fetches anything: the RO-Crate 1.3 context is embedded (pinned copy), IRIs are never dereferenced, and a referenced workspace is obtained only through a resolver passed explicitly by the caller. Its bytes are checked against the SHA-256 and size of the manifest before use. | `test_corpus.py` (sockets disabled), `test_revisions.py` |
| **Code execution from a file** | Nothing in a file is imported, evaluated or unpickled. A replay resolves an operation contract (`id`, version) through the edition's registry of qualified contracts and decodes parameters through that contract. Python names, class names and legacy records are kept as text. Attachments are returned as bytes and written only to a new file on explicit request (the CLI never overwrites); they are never opened, interpreted or executed. | `test_replay.py`, `test_legacy.py`, `test_attachments.py` |
| **HDF5 links and virtual datasets** pointing outside the file | External and soft links and virtual datasets are refused on every path the library reads; datasets are bounded (2 GiB per signal or image, 64 MiB per JSON text). | `test_hdf5_block.py` |
| **Tampering** with the manifest, workspace or stored data | The manifest fingerprint, the workspace SHA-256 and size, the SHA-256 and size of each attachment, the state fingerprints and ROI digests, and the match between the manifest and the workspace ledger are checked; any difference is reported. | corpus `tampered-manifest`, `workspace-mismatch`, `ledger-mismatch`, `altered-state`, `attachment-altered` |
| **Forged or misdirected reviews and reports** | A review's own fingerprint and its target revision are checked; cited reports must name activities of the capsule; reports are validated against their schema. | corpus `review-*`, `report-*` |
| **Misleading claims** about old results | Legacy records claim no result; they name the state observed at opening separately, and are never replayed. | `test_legacy.py` |
| **Leak of personal data** | Environment records hold no host or user names. | `test_environment.py` |

## Residual risks

- **No authenticity.** Fingerprints detect changes but anyone can recompute them after editing a capsule. A review names a reviewer without proving who wrote it. Signatures and authenticated identities are out of scope of profile 0.1.
- **HDF5 parsing.** Opening an untrusted `.h5` file runs the HDF5 C library through h5py; a vulnerability in that library could be reached before any of our checks. In DataLab-Web, the WebAssembly sandbox of Pyodide confines it. On the desktop, open capsules from trusted sources or with up-to-date h5py.
- **Memory.** The archive is held in memory and a stored signal or image may be read up to 2 GiB to verify its fingerprint. The default policy is an upper bound, not a tested capacity; each edition should pass a policy sized for its platform.
- **Data disclosure.** A capsule includes the whole workspace (data, metadata, titles), its attachments and the processing history, with timestamps and package versions. Users must review what they share; nothing is redacted.
- **Attachment content.** An attachment is checked for integrity only: its content is whatever the author put in it. A recipient who opens it with another application takes the risks of that application.
- **Text shown to users.** Names, statements, titles and parameter values come from the file. User interfaces must display them as plain text, never as HTML or rich text (in Qt, a `QLabel` interprets HTML unless its text format is set to plain text).
- **Dependencies.** `jsonschema` (and its dependencies), NumPy and h5py are trusted. Their versions are pinned by the editions and by the Pyodide distribution.

## Reporting

Report vulnerabilities privately to the DataLab maintainers, not in public issues.
