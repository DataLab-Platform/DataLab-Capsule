# Two-operator scenario

This scenario exchanges a processing chain between two operators, first without any server, then through an optional registry. Operator A works with DataLab Desktop, operator B with DataLab-Web in a browser; both use the same capsule dialog (_File > Workspace capsule…_, tabs _Export_, _Inspect_, _Local replay_, _Revisions_ and _Registry_). Nothing is computed when a capsule is checked; replays are done by each edition, locally.

The `workspace-capsule` command line is an independent tool for the same checks, without DataLab: it suits scripts and third parties, and each Desktop step below gives its command-line equivalent. Neither the command line nor a DataLab checkout is needed to follow the scenario in the interfaces.

| Step | Operator | Tool | Result |
| --- | --- | --- | --- |
| 1 | A | DataLab Desktop | Processing chain saved as a workspace with its provenance |
| 2 | A | DataLab Desktop, _Export_ tab (or `workspace-capsule create`) | Capsule with a light attachment (`protocol.txt`) |
| 3 | B | DataLab-Web, _File > Workspace capsule…_ | Inspection without opening, attachment saved on request, workspace opened, every activity replayed locally, processing continued, child revision exported, review written citing a report |
| 4 | A | DataLab Desktop, _Inspect_ and _Revisions_ tabs (or `workspace-capsule verify`, `diff`) | Child, review and report checked; revisions compared |
| 5 | A | DataLab Desktop, _Open as workspace_ and _Local replay_ tab | Child workspace opened and replayed on the Desktop |
| 6 (registry) | A, B | _Registry_ tab of both editions (or `workspace-capsule registry`) | Same exchange through a registry: A publishes, B retrieves A's revision and publishes the child and its review, A fetches and checks them |

## Prerequisites

- Operator A: DataLab (Desktop) with its capsule dialog. The command line is optional: `pip install datalab-capsule` for `validate`, `inspect`, `verify` and `diff`; `create` also needs `pip install "datalab-capsule[hdf5]"`, and the registry server `pip install "datalab-capsule[registry]"`.
- Operator B: DataLab-Web in Chromium, Firefox or Edge, with DataLab-Capsule available to the runtime.
- Exchanging capsules needs no server: without a registry, no account is needed and the capsule travels by any means (mail, shared folder, removable drive). DataLab Desktop and the command line then work without network access.
- DataLab-Web, however, cannot start offline: when it starts, the browser downloads its Python runtime (Pyodide) and packages from the network, and a few operations download an optional package on first use (OpenCV for one blob detection); see [interoperability](interoperability.md). Capsules and their data are never sent: checking, opening and replaying happen in the browser.
- The registry is optional: everything else works without it.

## Without server

### 1. Operator A: process and save (DataLab Desktop)

Process signals through DataLab's supported processors and save the workspace (_File > Save to HDF5 file…_), for example `chain.h5`. These executions are recorded with their parameters, inputs, outputs and environment. Keep the useful source objects and reference results: the record describes earlier states but does not archive every old array. Copying the final result alone to a new workspace does not reconstruct a verifiable analysis.

### 2. Operator A: create the capsule

In _File > Workspace capsule…_, _Export_ tab: enter a name (`chain`) and a description (`Operator A, DataLab Desktop`), _Add attachments…_ `protocol.txt`, then _Export capsule…_. The _Revisions_ tab now lists the exported revision.

Command-line equivalent, from the saved workspace:

```powershell
workspace-capsule create chain.h5 chain.dlcapsule --name chain --description "Operator A, DataLab Desktop" --attach protocol.txt
workspace-capsule inspect chain.dlcapsule
```

The capsule holds the workspace, its manifest and the attachment, declared with its size and SHA-256. Send `chain.dlcapsule` to operator B (mail, shared folder…).

Review data, metadata, titles, trial records and attachments before sending: there is no automatic minimal export or anonymization, and deleting an object does not remove its recorded traces. Acquisition files and external resources are not attached automatically. For a referenced capsule, deliver the matching HDF5 separately; neither its address nor the registry downloads it for the recipient.

### 3. Operator B: inspect, replay, continue, review (DataLab-Web)

In _File > Workspace capsule…_:

1. _Inspect_, _Choose a capsule…_: the integrity is computed from the files (manifest, workspace, stored states); nothing is opened or recomputed. The attachments are listed; _Save…_ writes one to disk, never opens it.
2. _Open as workspace_, then _Local replay_: _Replay_ recomputes each activity in the browser and compares it with the stored result (`exact`, `different`, or `not_verified` with its reason, e.g. an opaque operation). _Save report…_ keeps a verification report.
3. Continue the processing, then _Export_ with _Revision of chain_ checked: the child capsule names its parent by fingerprint. A child revision does not copy the parent's attachments: the export names those not selected again, to add with _Add attachments…_ if they still apply (the parent's attachment was saved in step 1).
4. _Inspect_ the child, write a review (reviewer, statement, verdict), _Cite a report…_, _Save the review…_.

Send the child capsule, the review and the report to operator A.

The proposed parent is the capsule whose workspace B opened, including after a save/reopen of its HDF5. Exporting a child does not move that origin: open A, export B, then export again still proposes A. Inspecting another capsule does not change the current workspace's parent. Attach the parent's files again deliberately; adding another workspace does not merge provenance or replace the parent.

### 4. Operator A: check and compare

In the _Inspect_ tab of DataLab Desktop: _Choose a capsule…_ `child.dlcapsule`, then _Add a verification report…_ `report.json` and _Add a review…_ `review.json`. The integrity of the child and each supplied document are shown apart; a review of another revision is refused. In the _Revisions_ tab, select `chain` and the child, then _Compare_.

Command-line equivalent:

```powershell
workspace-capsule verify child.dlcapsule --review review.json --report report.json
workspace-capsule verify chain.dlcapsule --review review.json   # refused: the review targets another revision
workspace-capsule diff chain.dlcapsule child.dlcapsule
```

The comparison sorts the changes into scientific, presentation, storage, environment and attachment changes, and states a scientific conclusion. A new workspace file (different bytes) is a storage change: it does not show a scientific change, and the comparison never concludes equality beyond what the manifests record. Attachment changes are listed without interpretation: a protocol or a calibration certificate may matter scientifically, the reader judges.

### 5. Operator A: replay on the Desktop

In the _Inspect_ tab, with the child still selected, _Open as workspace_: the child workspace replaces the current one. In the _Local replay_ tab, _Replay_ recomputes each activity separately and compares it with the stored result (`exact`, `within_tolerance`, `different`, or `not_verified` with its reason); the workspace is not changed. The activities added in DataLab-Web replay on the Desktop. _Save report…_ keeps a verification report.

In the main window, select a result and open the _Provenance_ tab of the Properties panel: it shows the recorded execution that produced it, its parameters and inputs (each expandable to its own production, as it was at the time); _Check data_ compares the data with their recorded states without computing, and _Verify computation_ recomputes the selected step separately, without changing the workspace. The _Processing_ tab restores the recorded call of the result (including two-signal and multi-object operations) and applies it again.

Without the dialog, `workspace-capsule workspace child.dlcapsule child.h5` extracts the checked workspace, which opens in DataLab like any workspace.

DataLab-Web offers the same consultation in the _Provenance_ tab of its side panel (step 3). In both editions, adding a workspace file to the current workspace (instead of replacing it) does not import its provenance; the editions say so before appending, and appended objects never inherit the provenance of another object.

## With a registry

The registry stores manifests, their declared attachments and reviews, never workspaces. It is started by whoever hosts it; publishing needs its token, reading does not.

```powershell
$env:REGISTRY_TOKEN = "<secret>"
workspace-capsule-registry registry-data --port 8765 --token-env REGISTRY_TOKEN --origin https://web.example
```

1. Operator A publishes the capsule's manifest and attachment (the workspace still travels as a file, or is referenced by an address). In DataLab Desktop, _Registry_ tab: registry address and publishing token (kept for the session only), _Use these settings_; then, in the _Inspect_ tab with `chain.dlcapsule` selected, _Publish to the registry_. Command-line equivalent:

   ```powershell
   workspace-capsule registry publish http://127.0.0.1:8765 chain.dlcapsule --token-env REGISTRY_TOKEN
   workspace-capsule registry list http://127.0.0.1:8765
   ```

2. Operator B, in the _Registry_ tab: registry address and token (kept for the session only), _Use these settings_, _List revisions_, _Retrieve_: the manifest is checked (valid, requested fingerprint) and added to the local revisions, reviews are shown with their status. After step 3 above, _Inspect_ the child, add the review, _Publish to the registry_.
3. Operator A fetches and checks the child and its reviews and attachments, then compares. In DataLab Desktop, _Registry_ tab: _List revisions_, select the child, _Retrieve_ (the child and its reviews are checked and added to the _Revisions_ tab, its attachments are offered after their check), then _Compare_ it with `chain` in the _Revisions_ tab. Command-line equivalent:

   ```powershell
   workspace-capsule registry list http://127.0.0.1:8765
   workspace-capsule registry fetch http://127.0.0.1:8765 <child fingerprint> fetched-child
   workspace-capsule diff child.dlcapsule fetched-child\manifest.json
   ```

Everything fetched is checked: the manifest against the requested fingerprint, each review against the manifest, each attachment against its declared size and SHA-256. Nothing is overwritten and no attachment is opened.

## Automated run

The scenario is also scripted for evidence. These scripts are developer tools: they need the repositories and their test dependencies, which the scenario above does not.

- DataLab `scripts/provenance_demo.py OUTDIR [OTHER.h5]` (operator A's chain and Desktop replays): it reopens each workspace in a fresh window, verifies every recorded activity (`DLMainWindow.verify_provenance_activity`), counts the real computations and writes the reports to `OUTDIR/desktop_reports.json`. `OUTDIR` must hold no evidence of an earlier run, unless `--reuse` keeps the chain workspace already built there (e.g. to verify the child revision derived from it). It needs a checkout of the DataLab repository and pytest, since it reuses DataLab's test helpers (`python scripts/run_with_env.py python scripts/provenance_demo.py OUTDIR child.h5` in a development checkout).
- The commands above, for operator A's checks.
- The opt-in DataLab-Web test `tests/e2e/two_operator_scenario.spec.ts` (operator B, real browser, through the dialog; `DLW_SCENARIO_DIR` holds `a/chain.dlcapsule`, outputs go to `b/`; `DLW_SCENARIO_REGISTRY` and `DLW_SCENARIO_TOKEN` add the registry steps).

The runs recorded in the [interoperability report](interoperability.md) were made with the command line and these scripts; the Desktop dialog came later.
