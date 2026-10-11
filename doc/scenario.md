# Two-operator scenario

This scenario exchanges a processing chain between two operators, first without any server, then through an optional registry. Operator A works with DataLab Desktop and the `workspace-capsule` command line; operator B works with DataLab-Web in a browser. Nothing is computed when a capsule is checked; replays are done by each edition, locally.

| Step | Operator | Tool | Result |
| --- | --- | --- | --- |
| 1 | A | DataLab Desktop | Processing chain saved as a workspace with its provenance |
| 2 | A | `workspace-capsule create` | Capsule with a light attachment (`protocol.txt`) |
| 3 | B | DataLab-Web, _File > Workspace capsule…_ | Inspection without opening, attachment saved on request, workspace opened, every activity replayed locally, processing continued, child revision exported, review written citing a report |
| 4 | A | `workspace-capsule verify`, `diff` | Child, review and report checked; revisions compared |
| 5 | A | `workspace-capsule workspace`, DataLab Desktop | Child workspace opened and replayed on the Desktop |
| 6 (registry) | A, B | `workspace-capsule registry`, Web _Registry_ tab | Same exchange through a registry: A publishes, B retrieves A's revision and publishes the child and its review, A fetches and checks them |

## Prerequisites

- Operator A: DataLab (Desktop) and DataLab-Capsule. The command line alone needs only `pip install DataLab-Capsule`; `create` also needs `pip install "DataLab-Capsule[hdf5]"`, and the registry `pip install "DataLab-Capsule[registry]"`.
- Operator B: DataLab-Web in Chromium, Firefox or Edge, with DataLab-Capsule available to the runtime.
- No account and no network are needed without the registry. The registry is optional: everything else works without it.

## Without server

### 1. Operator A: process and save (DataLab Desktop)

Process signals in DataLab and save the workspace (_File > Save to HDF5 file…_), for example `chain.h5`. Each processing is recorded with its parameters, inputs, outputs and environment.

### 2. Operator A: create the capsule

```powershell
workspace-capsule create chain.h5 chain.dlcapsule --name chain --description "Operator A, DataLab Desktop" --attach protocol.txt
workspace-capsule inspect chain.dlcapsule
```

The capsule holds the workspace, its manifest and the attachment, declared with its size and SHA-256. Send `chain.dlcapsule` to operator B (mail, shared folder…).

### 3. Operator B: inspect, replay, continue, review (DataLab-Web)

In _File > Workspace capsule…_:

1. _Inspect_, _Choose a capsule…_: the integrity is computed from the files (manifest, workspace, stored states); nothing is opened or recomputed. The attachments are listed; _Save…_ writes one to disk, never opens it.
2. _Open as workspace_, then _Local replay_: _Replay_ recomputes each activity in the browser and compares it with the stored result (`exact`, `different`, or `not_verified` with its reason, e.g. an opaque operation). _Save report…_ keeps a verification report.
3. Continue the processing, then _Export_ with _Revision of chain_ checked: the child capsule names its parent by fingerprint.
4. _Inspect_ the child, write a review (reviewer, statement, verdict), _Cite a report…_, _Save the review…_.

Send the child capsule, the review and the report to operator A.

### 4. Operator A: check and compare

```powershell
workspace-capsule verify child.dlcapsule --review review.json --report report.json
workspace-capsule verify chain.dlcapsule --review review.json   # refused: the review targets another revision
workspace-capsule diff chain.dlcapsule child.dlcapsule
```

`diff` sorts the changes into scientific, presentation, environment and attachment changes. Attachment changes are listed without interpretation: a protocol or a calibration certificate may matter scientifically, the reader judges.

### 5. Operator A: replay on the Desktop

```powershell
workspace-capsule workspace child.dlcapsule child.h5
python scripts/run_with_env.py python scripts/provenance_demo.py OUTDIR child.h5   # from the DataLab repository
```

`child.h5` opens in DataLab like any workspace. DataLab Desktop has no replay command in its interface yet: the script reopens the workspace in a fresh window and verifies every recorded activity (`DLMainWindow.verify_provenance_activity`), counting the real computations, and writes the reports to `OUTDIR/desktop_reports.json`. The activities added in DataLab-Web replay on the Desktop.

## With a registry

The registry stores manifests, their declared attachments and reviews, never workspaces. It is started by whoever hosts it; publishing needs its token, reading does not.

```powershell
$env:REGISTRY_TOKEN = "<secret>"
workspace-capsule-registry registry-data --port 8765 --token-env REGISTRY_TOKEN --origin https://web.example
```

1. Operator A publishes the capsule's manifest and attachment (the workspace still travels as a file, or is referenced by an address):

   ```powershell
   workspace-capsule registry publish http://127.0.0.1:8765 chain.dlcapsule --token-env REGISTRY_TOKEN
   workspace-capsule registry list http://127.0.0.1:8765
   ```

2. Operator B, in the _Registry_ tab: registry address and token (kept for the session only), _Use these settings_, _List revisions_, _Retrieve_: the manifest is checked (valid, requested fingerprint) and added to the local revisions, reviews are shown with their status. After step 3 above, _Inspect_ the child, add the review, _Publish to the registry_.
3. Operator A fetches and checks the child and its reviews and attachments, then compares:

   ```powershell
   workspace-capsule registry list http://127.0.0.1:8765
   workspace-capsule registry fetch http://127.0.0.1:8765 <child fingerprint> fetched-child
   workspace-capsule diff child.dlcapsule fetched-child\manifest.json
   ```

Everything fetched is checked: the manifest against the requested fingerprint, each review against the manifest, each attachment against its declared size and SHA-256. Nothing is overwritten and no attachment is opened.

## Automated run

The scenario is also scripted for evidence: DataLab `scripts/provenance_demo.py OUTDIR [OTHER.h5]` (operator A's chain and Desktop replays), the commands above (operator A), and the opt-in DataLab-Web test `tests/e2e/two_operator_scenario.spec.ts` (operator B, real browser, through the dialog; `DLW_SCENARIO_DIR` holds `a/chain.dlcapsule`, outputs go to `b/`; `DLW_SCENARIO_REGISTRY` and `DLW_SCENARIO_TOKEN` add the registry steps).
