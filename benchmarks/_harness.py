# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Common harness of the DataLab-Capsule benchmarks (not part of the package).

A benchmark module defines ``cases(profile) -> list[Case]`` and calls
:func:`main` from its ``__main__`` block. The harness gives every benchmark the
same command line and the same report (version 1, described in
``doc/performance.md``):

- ``--profile quick|representative|stress`` (default ``quick``), ``--repeat N``
  (default 5, after one warm-up run), ``--output PATH`` (parent folders are
  created), ``--case ID`` (repeatable), ``--list``;
- durations: every measured value is kept, with the median and the range (the
  minimum is secondary). Preparing the data of a case is never timed;
- memory, measured apart from durations so that instrumentation does not
  perturb them: ``tracemalloc`` (allocations traced during the operation, in
  this process), the RSS of a fresh child process running only that case,
  sampled from this process (CPython), or the WebAssembly heap capacity of the
  runtime (Pyodide). A measure that is not available is ``null`` with a
  reason, never 0;
- a status per case: ``ok``, ``refused_by_policy``, ``error``, ``timeout``,
  ``crash`` or ``unavailable`` (the case cannot run in this runtime, with the
  reason).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import gc
import importlib.metadata
import importlib.util
import json
import os
import os.path as osp
import platform
import statistics
import subprocess
import sys
import threading
import time
import traceback
import tracemalloc
import uuid
from collections.abc import Callable, Sequence
from typing import Any

REPORT_VERSION = 1
PROFILES = ("quick", "representative", "stress")
IS_PYODIDE = sys.platform == "emscripten"
PACKAGES = ("DataLab-Capsule", "numpy", "h5py", "jsonschema", "psutil")
MIB = 1024**2


@dataclasses.dataclass
class Case:
    """One measured operation.

    Attributes:
        id: Identifier, unique in a benchmark.
        profiles: Profiles that run this case.
        params: What the case is (shape, dtype, sizes...), copied to the report.
        setup: Prepares the input data (never timed); returns a context.
        run: The measured operation, called with the context.
        sizes: Sizes of the case (bytes and counts), from the context.
        check: Called with the context and the result of the warm-up run;
         raises AssertionError when the result is wrong.
        group: Cases of a group share their prepared context (setup once).
    """

    id: str
    profiles: tuple[str, ...]
    params: dict[str, Any]
    setup: Callable[[], Any]
    run: Callable[[Any], Any]
    sizes: Callable[[Any], dict[str, Any]] = lambda _context: {}
    check: Callable[[Any, Any], None] | None = None
    group: str | None = None


def unavailable(reason: str) -> dict[str, Any]:
    """Return the record of a measure that is not available."""
    return {"value": None, "reason": reason}


class Unavailable(Exception):
    """Raised by a case that cannot run here (e.g. no HTTP server in Pyodide)."""


def status_of(exc: BaseException) -> str:
    """Return the status of a case that raised *exc*."""
    if isinstance(exc, Unavailable):
        return "unavailable"
    if type(exc).__name__ == "CapsuleError" and "too large" in str(exc).lower():
        return "refused_by_policy"
    return "error"


def _versions(root: str | None) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    versions["commit"] = os.environ.get("DLC_BENCH_COMMIT")
    if versions["commit"] is None and root is not None and not IS_PYODIDE:
        try:
            versions["commit"] = subprocess.run(
                ["git", "-C", root, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            versions["dirty"] = bool(dirty)
        except (OSError, subprocess.CalledProcessError):
            pass
    return versions


def environment() -> dict[str, Any]:
    """Describe where the benchmark runs."""
    kind = os.environ.get("DLC_BENCH_RUNTIME") or (
        "pyodide" if IS_PYODIDE else "cpython"
    )
    env: dict[str, Any] = {
        "runtime_kind": kind,
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "host": os.environ.get("DLC_BENCH_HOST"),
    }
    if IS_PYODIDE:
        env["pyodide"] = os.environ.get("DLC_BENCH_PYODIDE")
    try:
        import psutil  # pylint: disable=import-outside-toplevel

        env["ram_bytes"] = psutil.virtual_memory().total
        freq = psutil.cpu_freq()
        env["cpu_max_mhz"] = None if freq is None else freq.max
    except (ImportError, OSError, NotImplementedError):
        env["ram_bytes"] = None
        env["ram_reason"] = "psutil not available"
    return env


def wasm_heap_bytes() -> int | None:
    """Return the capacity of the WebAssembly heap (Pyodide), or None.

    The capacity grows when the heap needs more memory and never shrinks: it is
    not the size of the live objects.
    """
    if not IS_PYODIDE:
        return None
    try:
        import pyodide_js  # pylint: disable=import-outside-toplevel,import-error

        return int(pyodide_js._module.HEAP8.length)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _durations(case: Case, context: Any, repeat: int) -> dict[str, Any]:
    durations = []
    for _index in range(repeat):
        gc.collect()
        start = time.perf_counter()
        result = case.run(context)
        durations.append((time.perf_counter() - start) * 1000)
        del result
    return {
        "durations_ms": [round(value, 3) for value in durations],
        "median_ms": round(statistics.median(durations), 3),
        "range_ms": [round(min(durations), 3), round(max(durations), 3)],
        "min_ms": round(min(durations), 3),
    }


def _tracemalloc(case: Case, context: Any) -> dict[str, Any]:
    gc.collect()
    tracemalloc.start()
    try:
        result = case.run(context)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del result
    return {"peak_bytes": peak, "after_bytes": current}


class RssSampler:
    """Sample the resident memory of a process (and its children) from outside.

    Args:
        pid: Process to observe.
        period_ms: Sampling period.
        tree: Add the resident memory of every descendant, read at the same
         instant (never the sum of peaks reached at different times).
    """

    def __init__(self, pid: int, period_ms: float, tree: bool = False) -> None:
        import psutil  # pylint: disable=import-outside-toplevel

        self.psutil = psutil
        self.process = psutil.Process(pid)
        self.period = period_ms / 1000
        self.tree = tree
        self.samples = 0
        self.peak = 0
        self.__stop = threading.Event()
        self.__thread = threading.Thread(target=self.__loop, daemon=True)

    def rss(self) -> int:
        """Return the resident memory now (bytes)."""
        processes = [self.process]
        if self.tree:
            processes += self.process.children(recursive=True)
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                pass
        return total

    def __loop(self) -> None:
        while not self.__stop.is_set():
            try:
                self.peak = max(self.peak, self.rss())
                self.samples += 1
            except self.psutil.NoSuchProcess:
                return
            self.__stop.wait(self.period)

    def start(self) -> None:
        """Start sampling."""
        self.__thread.start()

    def stop(self) -> None:
        """Stop sampling."""
        self.__stop.set()
        self.__thread.join()


def _load(module_path: str) -> Any:
    spec = importlib.util.spec_from_file_location("benchmark_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child(module_path: str, profile: str, case_id: str, conn: Any) -> None:
    """Run one case in a fresh process for the RSS measure."""
    try:
        cases = {case.id: case for case in _load(module_path).cases(profile)}
        case = cases[case_id]
        context = case.setup()
        gc.collect()
        conn.send(("ready", None))
        conn.recv()
        try:
            result = case.run(context)
            del result
            conn.send(("done", None))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            conn.send((status_of(exc), f"{type(exc).__name__}: {exc}"))
        conn.recv()
    except Exception:  # pylint: disable=broad-exception-caught
        conn.send(("error", traceback.format_exc(limit=3)))


def _rss(
    module_path: str, profile: str, case_id: str, period_ms: float, timeout: float
) -> dict[str, Any]:
    try:
        import multiprocessing  # pylint: disable=import-outside-toplevel

        import psutil  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        return unavailable("psutil not installed")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_child, args=(module_path, profile, case_id, child), daemon=True
    )
    process.start()
    record: dict[str, Any] = {"period_ms": period_ms, "method": "rss"}
    try:
        if not parent.poll(timeout):
            record["status"] = "timeout" if process.is_alive() else "crash"
            return record
        kind, detail = parent.recv()
        if kind != "ready":
            record.update(status=kind, error=detail)
            return record
        sampler = RssSampler(process.pid, period_ms)
        record["initial_bytes"] = sampler.rss()
        sampler.start()
        parent.send("go")
        finished = parent.poll(timeout)
        sampler.stop()
        if not finished:
            record["status"] = "timeout" if process.is_alive() else "crash"
        else:
            kind, detail = parent.recv()
            record["status"] = "ok" if kind == "done" else kind
            if detail:
                record["error"] = detail
            record["final_bytes"] = sampler.rss()
        record["peak_bytes"] = max(sampler.peak, record.get("final_bytes", 0))
        record["peak_delta_bytes"] = record["peak_bytes"] - record["initial_bytes"]
        record["samples"] = sampler.samples
        parent.send("exit")
        return record
    except (EOFError, OSError):
        record["status"] = "crash"
        return record
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
        record["exit_code"] = process.exitcode


def _measure(
    case: Case,
    context: Any,
    args: argparse.Namespace,
    module_path: str,
    setup_heap: tuple[int | None, int | None] = (None, None),
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": case.id,
        "params": case.params,
        "mode": {"execution": "in-process", "storage": None},
        "status": "ok",
    }
    try:
        heap_before = wasm_heap_bytes()
        result = case.run(context)
        heap_after = wasm_heap_bytes()
        if case.check is not None:
            case.check(context, result)
        del result
        entry["sizes"] = case.sizes(context)
        if args.phase in ("all", "timing"):
            entry.update(_durations(case, context, args.repeat))
        memory: dict[str, Any] = {}
        if args.phase in ("all", "memory"):
            memory["tracemalloc"] = _tracemalloc(case, context)
            if IS_PYODIDE:
                memory["rss"] = unavailable("not available in Pyodide")
            elif args.no_rss:
                memory["rss"] = unavailable("disabled (--no-rss)")
            else:
                memory["rss"] = _rss(
                    module_path, args.profile, case.id, args.sample_ms, args.timeout
                )
            if heap_before is None:
                memory["wasm_heap"] = unavailable("not a Pyodide runtime")
            else:
                memory["wasm_heap"] = {
                    "before_setup_bytes": setup_heap[0],
                    "after_setup_bytes": setup_heap[1],
                    "before_bytes": heap_before,
                    "after_first_run_bytes": heap_after,
                    "growth_bytes": heap_after - heap_before,
                    "note": "capacity of the heap of this runtime: it grows and "
                    "never shrinks; preparing the data (setup) may already have "
                    "grown it beyond what the operation needs",
                }
        entry["memory"] = memory
    except AssertionError as exc:
        entry.update(status="error", error=f"wrong result: {exc}")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        entry.update(status=status_of(exc), error=f"{type(exc).__name__}: {exc}")
    return entry


def parse_args(description: str, argv: Sequence[str] | None = None) -> Any:
    """Parse the common command line."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--profile", choices=PROFILES, default="quick")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--case", action="append", default=[], metavar="ID")
    parser.add_argument("--list", action="store_true", help="list the case ids")
    parser.add_argument(
        "--phase",
        choices=("all", "timing", "memory"),
        default="all",
        help="durations, memory, or both (default)",
    )
    parser.add_argument("--sample-ms", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--no-rss", action="store_true")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return args


def main(
    module_path: str,
    cases: Callable[[str], list[Case]],
    description: str,
    argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run the cases of a benchmark and write its report.

    Args:
        module_path: File of the benchmark module (``__file__``), imported again
         by the child processes of the RSS measure.
        cases: ``cases(profile)`` of the benchmark.
        description: First line of the help.
        argv: Arguments (default: the command line).

    Returns:
        The report.
    """
    args = parse_args(description, argv)
    selected = [case for case in cases(args.profile) if args.profile in case.profiles]
    if args.case:
        known = {case.id for case in selected}
        missing = [case_id for case_id in args.case if case_id not in known]
        if missing:
            raise SystemExit(f"unknown cases for {args.profile}: {missing}")
        selected = [case for case in selected if case.id in args.case]
    if args.list:
        print(json.dumps([case.id for case in selected]))
        return {}
    root = osp.dirname(osp.dirname(osp.abspath(module_path)))
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": str(uuid.uuid4()),
        "launcher": osp.basename(module_path),
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "profile": args.profile,
        "repeat": args.repeat,
        "phase": args.phase,
        "versions": _versions(None if IS_PYODIDE else root),
        "environment": environment(),
        "memory_methods": {
            "tracemalloc": "allocations traced during one run of the operation, "
            "in the measuring process; not the whole memory of the process",
            "rss": f"resident memory of a fresh process running only the case, "
            f"sampled every {args.sample_ms} ms from outside; a sampled peak is an "
            "observed peak",
            "wasm_heap": "capacity of the WebAssembly heap before and after the "
            "first run (Pyodide only)",
        },
        "cases": [],
    }
    contexts: dict[str, Any] = {}
    setup_heap: tuple[int | None, int | None] = (None, None)
    for case in selected:
        key = case.group or case.id
        try:
            if key not in contexts:
                contexts.clear()
                gc.collect()
                heap = wasm_heap_bytes()
                contexts[key] = case.setup()
                setup_heap = (heap, wasm_heap_bytes())
            entry = _measure(
                case, contexts[key], args, osp.abspath(module_path), setup_heap
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            entry = {
                "id": case.id,
                "params": case.params,
                "status": status_of(exc),
                "error": f"setup: {type(exc).__name__}: {exc}",
            }
        report["cases"].append(entry)
        _summary(entry)
    contexts.clear()
    report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    text = json.dumps(report, indent=1)
    if args.output:
        os.makedirs(osp.dirname(osp.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(text)
    else:
        print(text)
    return report


def _summary(entry: dict[str, Any]) -> None:
    parts = [f"{entry['id']}: {entry['status']}"]
    if "median_ms" in entry:
        parts.append(f"median {entry['median_ms']:.1f} ms")
    rss = (entry.get("memory") or {}).get("rss") or {}
    if rss.get("peak_delta_bytes") is not None:
        parts.append(f"RSS +{rss['peak_delta_bytes'] / MIB:.1f} MiB")
    peak = ((entry.get("memory") or {}).get("tracemalloc") or {}).get("peak_bytes")
    if peak is not None:
        parts.append(f"traced {peak / MIB:.1f} MiB")
    if entry.get("error"):
        parts.append(entry["error"])
    print(", ".join(parts), file=sys.stderr)
