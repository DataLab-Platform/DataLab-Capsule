# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Measure the time and memory cost of image fingerprints.

Run in CPython: ``python benchmarks/fingerprint_cost.py``.
Run in Pyodide: ``node tests/pyodide/run_benchmark.mjs <wheel>``.

For each image, prints the time of :func:`image_state_facts`, the time of a
plain SHA-256 over the same bytes (lower bound), and the peak memory allocated
during the call (``tracemalloc``, which tracks NumPy buffers) relative to the
image size.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import tracemalloc
from types import SimpleNamespace

import numpy as np

from datalab_capsule.integrity import image_state_facts

#: (rows, columns, dtype) of the measured images.
CASES = (
    (512, 512, "float64"),
    (2048, 2048, "uint16"),
    (2048, 2048, "float64"),
    (4096, 4096, "float64"),
)


def _image(rows: int, cols: int, dtype: str) -> SimpleNamespace:
    data = np.random.default_rng(0).random((rows, cols)) * 1000
    return SimpleNamespace(
        data=data.astype(dtype),
        is_uniform_coords=True,
        x0=0.0,
        y0=0.0,
        dx=1.0,
        dy=1.0,
        xcoords=None,
        ycoords=None,
        xunit="mm",
        yunit="mm",
        zunit="",
        roi=None,
    )


def _best(func, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - start)
    return best


def measure(repeat: int = 3) -> list[dict]:
    """Return one measurement per case."""
    results = []
    for rows, cols, dtype in CASES:
        obj = _image(rows, cols, dtype)
        size = obj.data.nbytes
        facts_s = _best(lambda: image_state_facts(obj), repeat)
        sha_s = _best(lambda: hashlib.sha256(memoryview(obj.data)).digest(), repeat)
        tracemalloc.start()
        image_state_facts(obj)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        results.append(
            {
                "image": f"{rows}x{cols} {dtype}",
                "mib": round(size / 2**20, 1),
                "facts_ms": round(facts_s * 1000, 1),
                "sha256_ms": round(sha_s * 1000, 1),
                "peak_mib": round(peak / 2**20, 1),
                "peak_ratio": round(peak / size, 2),
            }
        )
    return results


def main() -> str:
    """Print and return the measurements as JSON."""
    text = json.dumps({"python": sys.version.split()[0], "results": measure()})
    print(text)
    return text


if __name__ == "__main__":
    main()
