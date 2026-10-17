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

To be completed with the measured campaigns.
