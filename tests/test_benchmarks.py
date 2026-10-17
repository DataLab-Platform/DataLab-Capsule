# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The benchmark launchers produce complete reports (one small case each)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).parents[1] / "benchmarks"
pytestmark = pytest.mark.skipif(
    not BENCHMARKS.is_dir() or sys.platform == "emscripten",
    reason="benchmarks are not part of this test run",
)


def _load(name: str):
    sys.path.insert(0, str(BENCHMARKS))
    spec = importlib.util.spec_from_file_location(name, BENCHMARKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name, case",
    [("fingerprint_cost", "image-512-float64-roi"), ("capsule_cost", "ws-6mib.verify")],
)
def test_report(name: str, case: str, tmp_path: Path) -> None:
    """A case gives a version 1 report with raw durations and explicit nulls."""
    module = _load(name)
    harness = _load("_harness")
    output = tmp_path / "results" / "report.json"
    argv = ["--case", case, "--repeat", "2", "--no-rss", "--output", str(output)]
    harness.main(module.__file__, module.cases, name, argv)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["report_version"] == 1 and report["profile"] == "quick"
    assert report["environment"]["runtime_kind"] == "cpython"
    (entry,) = report["cases"]
    assert entry["id"] == case and entry["status"] == "ok", entry
    assert len(entry["durations_ms"]) == 2
    assert entry["range_ms"][0] <= entry["median_ms"] <= entry["range_ms"][1]
    memory = entry["memory"]
    assert memory["tracemalloc"]["peak_bytes"] >= 0
    assert memory["rss"] == {"value": None, "reason": "disabled (--no-rss)"}
    assert memory["wasm_heap"]["value"] is None


def test_unknown_profile_and_case_are_refused() -> None:
    """An unknown profile or case stops the launcher."""
    harness = _load("_harness")
    module = _load("fingerprint_cost")
    with pytest.raises(SystemExit):
        harness.parse_args("x", ["--profile", "huge"])
    with pytest.raises(SystemExit, match="unknown cases"):
        harness.main(module.__file__, module.cases, "x", ["--case", "image-4096"])
