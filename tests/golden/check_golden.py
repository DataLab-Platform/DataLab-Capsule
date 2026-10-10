# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Golden-vector checks shared by pytest (CPython) and the Pyodide runner.

Only the standard library, NumPy and :mod:`datalab_capsule` are used, so the
same file runs unchanged in both interpreters.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

from datalab_capsule.integrity import (
    canonical_json,
    image_fingerprint,
    json_digest,
    signal_fingerprint,
)


def _double(bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(bits))[0]


def run(vectors_path: str) -> list[str]:
    """Check every golden vector and return the list of failures."""
    base = Path(vectors_path).parent
    doc = json.loads(Path(vectors_path).read_text(encoding="utf-8"))
    failures: list[str] = []
    for case in doc["numbers"]:
        got = canonical_json(_double(case["bits"]))
        if got != case["expected"]:
            failures.append(f"number {case['bits']}: {got!r} != {case['expected']!r}")
    for case in doc["jcs_files"]:
        value = json.loads((base / case["input"]).read_text(encoding="utf-8"))
        expected = (base / case["expected"]).read_bytes().rstrip(b"\r\n")
        got = canonical_json(value).encode("utf-8")
        if got != expected:
            failures.append(f"jcs {case['input']}: {got!r} != {expected!r}")
    order = doc["key_order"]
    keys = [
        k for k, _ in json.loads(canonical_json(order["input"]), object_pairs_hook=list)
    ]
    if keys != order["expected"]:
        failures.append(f"key order: {keys!r}")
    for case in doc["fingerprints"]:
        if "y_hex" in case:
            y = [_double(bits) for bits in case["y_hex"]]
        else:
            y = case["y"]
        got = signal_fingerprint(
            np.array(case["x"], dtype=np.float64),
            np.array(y, dtype=np.float64),
            xunit=case["units"]["x"],
            yunit=case["units"]["y"],
        )
        if got != case["expected"]:
            failures.append(f"fingerprint {case['name']}: {got}")
    for case in doc.get("image_fingerprints", []):
        coords = {
            k: np.array(v, dtype=np.float64) if isinstance(v, list) else v
            for k, v in case["coords"].items()
        }
        got = image_fingerprint(
            np.array(case["data"], dtype=case["dtype"]), **coords, **case["units"]
        )
        if got != case["expected"]:
            failures.append(f"image fingerprint {case['name']}: {got}")
    for case in doc["digests"]:
        got = json_digest(case["document"])
        if got != case["expected"]:
            failures.append(f"digest {case['name']}: {got}")
    return failures
