# Performance and capacity

This page says how the cost of provenance and capsules is measured in DataLab-Capsule, DataLab Desktop and DataLab-Web, what each measure covers, and what was measured. Results are those of the environment described with them: a duration or a memory peak measured on one computer is a trend, not a guarantee for another one.

## What is measured, and with what

| Measure | Used for | What it does not cover |
| --- | --- | --- |
| Durations (`time.perf_counter`, `performance.now`) | One operation, after one warm-up run; every value is kept, with the median and the range | Preparing the input data is never timed |
| `tracemalloc` | Allocations traced during one run of an operation, in the measuring process | Not the whole memory of the process, nor the JavaScript or WebAssembly memory |
| RSS of a process (CPython) | Resident memory of a fresh process running only that case, sampled from outside (psutil) | Allocators may keep memory: a sampled peak is an observed peak |
| RSS of the Desktop process tree | Application and computation processes, summed at the same instant | Never a sum of peaks reached at different times |
| WebAssembly heap capacity (Pyodide) | Growth of the heap of the runtime during an operation | Capacity, which never shrinks: not the size of the live objects, nor the memory of the browser |
| Live data and JavaScript heap (browser) | Complement in DataLab-Web, when the browser gives it | Not the whole memory of the browser; availability depends on the browser |

Memory is measured apart from durations, in a new process (CPython) or a new Pyodide runtime (Node), so that instrumentation does not perturb durations and earlier cases do not hide later ones. A measure that is not available is `null` with a reason, never 0. Pyodide under Node gives the cost of the Python code in WebAssembly: it is not a measure of DataLab-Web in a browser, which is measured by its own end-to-end tests.

## Report format (version 1)

Every launcher writes one JSON report:

- `report_version` (1), `run_id`, `launcher`, `started_at`, `finished_at`, `profile`, `repeat`, `phase`;
- `versions`: the commit of the repository (and whether it had local changes) and the versions of the packages used;
- `environment`: `runtime_kind` (`cpython`, `pyodide-node`, `desktop`, `browser-main`, `browser-worker`), Python and Pyodide versions, platform, processor, number of CPUs, RAM, host description;
- `memory_methods`: what each memory measure means in this run;
- `cases`, each with:
  - `id` and `params` (what the case is);
  - `mode`: execution and storage modes;
  - `sizes`: arrays, HDF5 file, archive and manifest, objects, states, activities, compressibility. These sizes are measured separately, because a compressed or chunked HDF5 file is not the size of the arrays it holds;
  - `durations_ms`, `median_ms`, `range_ms`, `min_ms`;
  - `memory`;
  - `status`: `ok`, `refused_by_policy`, `error`, `timeout`, `crash` or `unavailable`, with `error` when not `ok`.

A case that was required but not run is reported as missing, never replaced by an older result.

## Launchers

| Launcher | Repository | Measures |
| --- | --- | --- |
| `benchmarks/fingerprint_cost.py` | DataLab-Capsule | State fingerprints of images and signals (several dtypes, non-contiguous and big-endian arrays, ROI), with a plain SHA-256 as a lower bound for comparison |
| `benchmarks/capsule_cost.py` | DataLab-Capsule | Creation, reading, validation and verification of capsules, the `exchange` functions used by both interfaces, a verified registry download, referenced workspaces |
| `tests/pyodide/run_benchmark.mjs` | DataLab-Capsule | The two benchmarks above in Pyodide under Node |
| `scripts/benchmark_provenance.py` | DataLab | Cost of provenance capture in DataLab Desktop, saving and reopening, consultation, checks and verification, capsule export, inspection and opening through the Desktop adapter, unavailability of the Qt interface |
| `tests/e2e/provenance_capsule_perf.spec.ts` | DataLab-Web | The same user paths in a browser, main thread or worker, RAM or disk storage |

The Python launchers share their options: `--profile quick|representative|stress` (default `quick`), `--repeat N` (default 5), `--output PATH` and `--case ID`. An unknown profile or case stops the launcher. Results are written outside the repositories' tracked files (for example under `build/`); generated workspaces and capsules are never committed.

Profiles:

- `quick`: small data, to check that the launchers work and report complete results;
- `representative`: about 34 and 136 MiB of arrays, a ledger of 1000 activities;
- `stress`: close to the DataLab-Web limit (about 234 MiB), on a suitable computer and on demand.

## Results

### Reference computer and versions

Campaign of 17 October 2026, on one laptop:

- Windows 11 (build 26200), Intel Core i5-1335U (10 cores, 12 threads), 32 GiB of RAM;
- CPython 3.12.10 for DataLab-Capsule and 3.11.6 for DataLab Desktop (Qt 5.15.2, computations in a separate process as by default), Pyodide 0.26.4 (Python 3.12.1) under Node 22.23.2, Chromium 153 driven by Playwright;
- development commits shortly before the releases (branch `feature/workspace-provenance`, since merged into `develop` and released as DataLab-Capsule 0.1.0, Sigima 1.3.0 and DataLab-Web 0.10.0): DataLab-Capsule `57989f3` (with the verification fix below), DataLab `30fa1d01`, DataLab-Web `56b5f96`, Sigima `9edf2c3`.

Profiles run: `quick` on every launcher, `representative` on CPython, Pyodide under Node, DataLab Desktop and DataLab-Web (worker, RAM and disk), the main thread of DataLab-Web as a diagnostic, and the `capacity` profile of DataLab-Web; `stress` was only run on three cases in CPython, to check the launcher. Durations are medians of 5 runs after a warm-up (3 for the browser), on a laptop that is not isolated: they give orders of magnitude, not guarantees. The raw reports are kept with the delivery, not in this repository.

### Fingerprints

The fingerprint of a state costs what a plain SHA-256 of its arrays costs: the scientific fingerprint adds nothing measurable to hashing the data. A non-contiguous or big-endian array is copied in bounded chunks (8 MiB), never as a whole.

| Case | CPython | Pyodide (Node) |
| --- | --- | --- |
| Image 2048² float64 (32 MiB) | 16.7 ms (SHA-256 alone: 16.1 ms) | 109 ms (105 ms) |
| Same image, transposed (not contiguous) | 70 ms, 16 MiB copied in chunks | 155 ms |
| Same image, big-endian | 25 ms, 16 MiB copied in chunks | 112 ms |
| Signal of 1 M points, float64 | 8.3 ms | 52 ms |
| Signal of 1 M points, complex128 | 12.6 ms | 75 ms |
| ROI definition of 100 regions | 1.7 ms | 3.2 ms |

### Capsules (DataLab-Capsule)

Generated chains of images (a uint16 source, then float64 results; compressibility 1.09), CPython / Pyodide under Node:

| Arrays (archive) | Read | Verify | Inspect | Open | Memory of a verification (CPython) |
| --- | --- | --- | --- | --- | --- |
| 6.5 MiB (6.6 MiB) | 9.5 / 29 ms | 28 / 92 ms | 42 / 143 ms | 10 / 31 ms | +6.5 MiB |
| 34 MiB (34 MiB) | 40 / 132 ms | 99 / 390 ms | 142 / 528 ms | 44 / 133 ms | +42 MiB |
| 136 MiB (136 MiB) | 151 / 517 ms | 349 / 1457 ms | 505 / 1981 ms | 157 / 515 ms | +168 MiB |
| 234 MiB (CPython only) | — | 823 ms | 1157 ms | 338 ms | +242 MiB |
| 1000 activities, 1.6 MB of arrays | 190 / 659 ms | 1665 / 7961 ms | 1989 / 5675 ms | 330 / 1076 ms | +30 MiB |

- Reading and opening hold the archive in memory once (resident memory grows by about the archive size); verifying adds the stored states read with h5py. Creating a capsule holds the workspace and the archive (+265 MiB for 136 MiB).
- `exchange.inspect_capsule` reads the archive twice (`inspect_archive_reads`: 2): it costs about 45 % more time than `verify_capsule`, with the same peak memory, since the first copy is released before the second read. This is kept as it is.
- The cost of a long ledger was dominated by repeated work: verifying 1000 activities took 4.0 s (inspection 5.0 s) in CPython before the manifest fingerprint was computed once instead of five times and the provenance block read once instead of twice; it now takes 1.7 s (2.0 s). The format and the results are unchanged. In Pyodide, the ledger cases vary from run to run (inspection measured faster than verification): their order of magnitude only is meaningful.
- A referenced workspace supplied apart is not part of the archive: opening it costs about half (77 ms instead of 157 ms for 136 MiB).

### DataLab Desktop

Representative profile (computations in a separate process):

| Path | Result |
| --- | --- |
| Capture, light operation (constant added to 100 000 points) | 47.0 ms with capture, 48.5 ms without: no measurable difference |
| Capture, Gaussian filter of a 2048² float64 image | 479 ms with, 449 ms without: +29 ms (+6.5 %), the fingerprints of the input and of the result |
| Capture, average of 5 images 1024² | 268 ms with, 243 ms without: +26 ms (+11 %) |
| Saving / reopening a chain of 4 images 1024² (32 MiB) | 79 ms / 159 ms |
| Consulting the provenance of the result | 0.13 ms (no computation) |
| Checking its data / verifying its step | 17.5 ms / 99 ms (*Exact*) |
| Verifying a step whose input state is gone | 9.6 ms, *Not verified*, nothing recomputed |
| Capsule export / opening (32 MiB) | 238 ms / 519 ms, resident memory +96 / +72 MiB |
| Capsule dialog: inspection / opening | 161 ms / 282 ms; interface unavailable 165–195 ms |

With a ledger of 1000 activities on small signals (1001 objects), measured again on 18 October 2026 after the fix of the object panel described below: consulting a result stays at 0.1–0.2 ms; saving takes 11.5 s (manifest 2.5 MB), exporting 13.4 s; opening the capsule dialog makes the interface unavailable for 0.4 s (0.6 s once), listing the 1000 activities of the Local replay tab 0.33 s. Building that workspace (1000 processings in a row) takes 2.8 minutes. Each processing still costs a little more as the panel grows (about 80 ms at the start, 0.26 s with 1000 objects), with or without provenance: the remaining cost is the refresh of the plot, which goes through the items of every object of the panel. Building it first took 39 minutes (2.3 s per processing at the end), and again 15 minutes once titles referred to other objects by identifier: each new object made the panel measure every title, resolving its references by scanning the whole workspace. Provenance never appeared among these costs.

The benchmark first showed that opening, inspecting or exporting a capsule of a few MiB allocated 4 GiB (+2 GiB of resident memory, 0.6 s): the size-checked read of the Desktop asked for a buffer of the size limit. Fixed: the same operations now take 47–178 ms and +8 to +24 MiB.

### DataLab-Web

Representative profile, worker (UI thread as a diagnostic), chain of 4 images 2048² (128 MiB of arrays, HDF5 file and capsule of 128 MiB):

| Path | Worker, RAM | Worker, disk | UI thread, RAM |
| --- | --- | --- | --- |
| Gaussian filter, captured | 832 ms | 1351 ms | 667 ms |
| Saving / reopening the workspace | 1.8 s / 1.7 s | 1.9 s / 2.2 s | 1.1 s / 1.0 s |
| Consulting the provenance of the result | 1.5 ms | 1.4 ms | 1.0 ms |
| Checking its data | 1.0 s | 1.5 ms (data on disk reported as not loaded, never paged in) | 0.7 s |
| Verifying its step | 1.0 s (*Exact*) | 1.3 s | 0.9 s |
| Capsule export / inspection / opening | 2.6 s / 3.3 s / 1.8 s | 2.4 s / 3.4 s / 2.6 s | 1.9 s / 3.4 s / 1.9 s |
| WebAssembly heap after these paths | 988 MiB | 958 MiB | 988 MiB |

- Opening a capsule over a current workspace costs the same as opening it in an empty one: replacing the workspace adds nothing measurable.
- Three cycles of opening, closing and freeing leave the heap at the same capacity: freed memory is reused.
- With a ledger of 1000 activities: building it took 4.9 s, saving 9.6 s, exporting 14 s; consulting one result 3.6–14 ms; listing the 1000 activities of the Local replay tab 0.58 s, with a longest task of 0.26 s on the UI thread.
- Capacity profile (222 MiB of arrays, HDF5 file and capsule of 233 MiB, close to the 256 MiB limit): every path works; the heap reached 1515 MiB (1.48 GiB) at the end of the session, below the 4 GiB ceiling of 32-bit WebAssembly; inspection takes 5.7 s and opening 3.1 s.
- Starting the runtime took 20 to 70 s, downloads included (the browser cache was not controlled).

## Answers

1. **Latency added by capture to a light operation.** None measurable on the Desktop (47.0 ms with, 48.5 ms without). Capture costs the fingerprints of the inputs and results, about the time of a SHA-256 of their arrays: +29 ms for a 32 MiB image filtered in 450 ms.
2. **Memory needed to open a capsule.** The size of the archive once more in CPython and on the Desktop (+136 MiB for 136 MiB, +72 MiB for 32 MiB on the Desktop); in the browser, a session working with a 233 MiB capsule raised the WebAssembly heap to 1.5 GiB. The observation recorded earlier in DataLab-Web (+841 MiB of heap to open a 240 MiB capsule) was not measured again on its own: it remains an observation in its environment, not a coefficient.
3. **Is consultation local to what the user opens?** Yes. Consulting a result costs 0.1–14 ms with 4 or 1000 activities, in both editions, never computes, and never loads data stored on disk. Checking data reads only the arrays of the result and of its direct inputs; listing every activity (Local replay tab) depends on their number.
4. **How long the Qt interface is unavailable.** 165–195 ms for the inspection or opening of a 32 MiB capsule; 0.33 s to list 1000 activities, 0.4 s to open the dialog on such a workspace (0.6 s once). No change of the threading of the dialog is needed for these sizes.
5. **Recommended range on the reference computer** — see below.

## Limits: configured, tested, recommended

| Edition | Configured limit | Largest case tested | Recommended range on the reference computer |
| --- | --- | --- | --- |
| DataLab-Capsule (library, command line) | Workspace 4 GiB, manifest 64 MiB, 8 attachments of 16 MiB: an accepted upper bound, not a tested capacity | 234 MiB (verification 0.8 s, +242 MiB) | Up to a few hundred MiB per capsule, with free memory of about twice the archive |
| DataLab Desktop | The library limits; files are checked before being read | 32 MiB in the interface, 234 MiB through the library; 1000 activities | Tested in the interface up to 32 MiB (unavailable less than 0.2 s); the wait grows with the size of the capsule, roughly in proportion through the library (not measured in the interface beyond 32 MiB). Tested up to 1000 objects per panel (each processing then takes about 0.26 s, against 80 ms in an empty panel) |
| DataLab-Web | Archive and workspace 256 MiB, manifest 16 MiB | 233 MiB (heap 1.5 GiB in a session) | Up to about 130 MiB per capsule for an interactive use (operations of 1 to 3.5 s); up to the 256 MiB limit with patience (5.7 s to inspect 233 MiB) and a browser that can give the page about 2 GiB |

There is no fixed maximum activity count, but the ledger is not unbounded: JSON volume limits and structural protections still apply. The measured 1000 activities stay usable in both editions (verification of their capsule 1.7 s in CPython, 8 s in Pyodide under Node); this is not a promise for arbitrary ledgers.

### Technical bounds

| Resource | Current bound and source |
| --- | --- |
| Library/Desktop archive | `MAX_ARCHIVE_BYTES`: 4 GiB workspace + 64 MiB manifest + 8 × 16 MiB attachments + 1 MiB ZIP headers; distinct from the workspace limit (`archive.py`) |
| Archive structure | At most 16 entries in prescribed locations, stored without ZIP compression; internal HDF5 compression is a separate matter (`archive.py`) |
| Stored-state fingerprint reads | At most 2 GiB per signal or image (`hdf5.py`); not a guaranteed practical working size |
| HDF5 JSON texts | At most 64 MiB each; JSON nesting depth 64; ROI definitions limited to 10 000 nodes read (`hdf5.py`, `integrity.py`) |
| Per-result projection | 200 input entries per activity, 50 analyses and 20 states per object, expansion depth 64 (`inspection.py`); truncation does not delete records |
| Edition summaries | 120 characters and up to 8 collection items; the record can contain more than the displayed text |
| Local revisions | 100 manifests per edition (`capsulestore.py`, `capsuleRevisions.ts`), not a backup of capsule files |
| Reference registry | Manifest 8 MiB, review 1 MiB, attachment 16 MiB, at most 1000 revisions in a listing (`registry.py`) |

The Web archive and workspace each remain limited to 256 MiB, and its manifest to 16 MiB. Policies differ: acceptance by one edition does not imply acceptance by another. Creation can retain several representations of the archive and HDF5; none of these bounds is a universal memory multiplier.

These ranges are those of one computer. They are neither guarantees nor limits enforced by the editions: the configured limits stay as they are, and no edition aligns its limits on the other one.

## Delivery checklist (17 October 2026)

Documentation and distribution:

- [x] The user guides follow both interfaces and state the measured limits (DataLab `provenance.rst` and `capsules.rst`, DataLab-Web `provenance-and-capsules.md`, [scenario](scenario.md)).
- [x] User instructions need no development checkout: the scenario goes through both interfaces, the command line is optional, the evidence scripts are presented as developer tools.
- [x] Versions, embedded resources, profile and corpus agree: the schemas and context of the candidate wheel have the SHA-256 published by the profile (`check_profile.py`, also checked by `check_dist.py --profile`).
- [x] Candidate distributions installed and used in clean environments: DataLab-Capsule (core and extras), DataLab Desktop (wheels, then PyInstaller build), DataLab-Web (production bundle from its tarball).
- [ ] To do: the same checks with the published packages only (after DataLab-Capsule 0.1.0, Sigima 1.3.0, guidata 3.16.0 and the DataLab-Web pins); the MSI installer; DataLab 1.4.0 (outside the October sequence).
- [x] Scenario replayed with the candidate artefacts, the command-line checks and the registry variant; the Desktop part through its dialog and the Web part by hand remain to be replayed by an operator ([ ] to do).
- [ ] Not delivered: conda-forge (recipe of DataLab-Capsule prepared and built locally, update of the DataLab recipe prepared; both to be submitted after the PyPI publications).

Costs and capacity:

- [x] Fingerprints, capsules, capture overhead and user paths measured, with the same report format in every launcher.
- [x] Arrays, HDF5 file, archive, Python memory, WebAssembly heap and observed JavaScript memory are reported apart.
- [x] Consulting a result neither computes nor loads arrays (DataLab-Web disk mode checked in the measures; computation counters and existing tests on the Desktop).
- [x] Limits cover referenced workspaces, the Desktop reads and the Web export (refused before reading or before building the archive).
- [x] Unavailability of the Qt dialog measured; no change of its threading is justified by the sizes measured.
- [x] Configured limits, largest cases tested and recommended ranges are given apart.
- [x] Each optimization has its before/after measure (verification of long ledgers, bounded reads of the Desktop); nothing was redesigned.
