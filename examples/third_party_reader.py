# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Read and check a DataLab workspace capsule without DataLab-Capsule.

A third-party example: it uses the Python standard library, h5py and NumPy only,
and follows the published profile (https://datalab-platform.com/capsule/0.1).
It checks, without computing anything:

1. the manifest fingerprint (SHA-256 of the RFC 8785 canonical JSON);
2. the SHA-256 and size of the workspace (included, or given as a file);
3. the fingerprint of every state stored in the workspace
   (``datalab-signal-v1``, ``datalab-signal-v2``, ``datalab-image-v1``);

then prints the activities with their roles and parameters.

It is not a hardened reader: it reads the two expected entries by name, in
memory, but does not apply the size and safety limits of
``datalab_capsule.archive.read_capsule``. Use that function for untrusted files.

Usage::

    python third_party_reader.py CAPSULE [WORKSPACE]
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import zipfile
from decimal import Decimal

import h5py
import numpy as np

MANIFEST = "ro-crate-metadata.json"
WORKSPACE = "workspace.h5"
FINGERPRINT = "dlc:manifestFingerprint"


# --- RFC 8785 canonical JSON -------------------------------------------------


def _number(value: float) -> str:
    """ECMAScript ``Number.prototype.toString`` (shortest round-trip digits)."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError("Non-finite number")
    if value == 0.0:
        return "0"
    sign = "-" if value < 0 else ""
    _sign, digits_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(map(str, digits_tuple))
    k, n = len(digits), len(digits) + exponent
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        text = f"{mantissa}e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"
    return sign + text


def canonical(value) -> str:
    """Return the RFC 8785 form of a JSON value.

    Strings use the same escapes as ``json.dumps(..., ensure_ascii=False)``;
    integers are written as such (exact below 2**53, as in capsule manifests).
    """
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
    return "{" + ",".join(f"{canonical(k)}:{canonical(v)}" for k, v in items) + "}"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- State fingerprints --------------------------------------------------------


def _text(node, name: str) -> str:
    value = node.attrs.get(name, "")
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _le(array, dtype) -> bytes:
    return np.ascontiguousarray(
        array, dtype=np.dtype(dtype).newbyteorder("<")
    ).tobytes()


def signal_fingerprint(node) -> str:
    """``datalab-signal-v1`` or ``-v2``: rows x, y (and dx, dy unless all NaN).

    The X and dX rows are real numbers (real part, as float64); the Y and dY
    rows keep the stored dtype. When all rows share one dtype the scheme is
    ``datalab-signal-v1``, otherwise ``datalab-signal-v2`` (one dtype per row).
    """
    data = node["xydata"][()]
    names = ["x", "y"]
    rows = [data[0].real.astype(np.float64), data[1]]
    for index, name in ((2, "dx"), (3, "dy")):
        if data.shape[0] > index and not np.all(np.isnan(data[index])):
            names.append(name)
            row = data[index]
            rows.append(row.real.astype(np.float64) if name == "dx" else row)
    dtypes = [row.dtype.newbyteorder("<") for row in rows]
    units = {"x": _text(node, "xunit"), "y": _text(node, "yunit")}
    if len(set(dtypes)) == 1:
        descriptor = {
            "scheme": "datalab-signal-v1",
            "dtype": dtypes[0].str,
            "length": int(data.shape[1]),
            "rows": names,
            "units": units,
        }
    else:
        descriptor = {
            "scheme": "datalab-signal-v2",
            "length": int(data.shape[1]),
            "rows": [{"name": n, "dtype": d.str} for n, d in zip(names, dtypes)],
            "units": units,
        }
    payload = canonical(descriptor).encode("utf-8") + b"\x00"
    return sha256(
        payload + b"".join(_le(row, dtype) for row, dtype in zip(rows, dtypes))
    )


def image_fingerprint(node) -> str:
    """``datalab-image-v1``: data in C order, then non-uniform coordinates."""
    data = node["data"][()]
    dtype = data.dtype.newbyteorder("<")
    extra = b""
    if bool(node.attrs.get("is_uniform_coords", True)):
        coords = {
            "uniform": {
                name: float(node.attrs.get(name, default))
                for name, default in (
                    ("x0", 0.0),
                    ("y0", 0.0),
                    ("dx", 1.0),
                    ("dy", 1.0),
                )
            }
        }
    else:
        xc, yc = node["xcoords"][()], node["ycoords"][()]
        coords = {"nonuniform": {"x": int(xc.size), "y": int(yc.size)}}
        extra = _le(xc, np.float64) + _le(yc, np.float64)
    descriptor = {
        "scheme": "datalab-image-v1",
        "dtype": dtype.str,
        "shape": [int(n) for n in data.shape],
        "coords": coords,
        "units": {k: _text(node, f"{k}unit") for k in ("x", "y", "z")},
    }
    payload = canonical(descriptor).encode("utf-8") + b"\x00"
    return sha256(payload + _le(data, dtype) + extra)


# --- Capsule -----------------------------------------------------------------


def check_capsule(path: str, workspace_path: str | None = None) -> dict:
    """Check a capsule; return what was found (see the module docstring)."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
        names = archive.namelist()
        workspace = archive.read(WORKSPACE) if WORKSPACE in names else None
    graph = {entity["@id"]: entity for entity in manifest["@graph"]}
    root = graph["./"]
    unsigned = json.loads(json.dumps(manifest))
    next(e for e in unsigned["@graph"] if e["@id"] == "./").pop(FINGERPRINT)
    result = {
        "manifest_ok": sha256(canonical(unsigned).encode("utf-8")) == root[FINGERPRINT],
        "revision_of": root.get("dlc:revisionOf"),
        "workspace": "absent",
        "states": {},
        "activities": [],
    }
    file_entity = graph[root["hasPart"][0]["@id"]]
    if workspace is None and workspace_path is not None:
        with open(workspace_path, "rb") as file:
            workspace = file.read()
    if workspace is not None:
        # RO-Crate's sha256 holds the bare hexadecimal digest; contentSize is
        # a string, as schema.org recommends.
        ok = hashlib.sha256(workspace).hexdigest() == file_entity["sha256"] and str(
            len(workspace)
        ) == str(file_entity["contentSize"])
        result["workspace"] = "ok" if ok else "mismatch"
        if ok:
            with h5py.File(io.BytesIO(workspace), "r") as h5file:
                for entity in graph.values():
                    if "dlc:locator" not in entity:
                        continue
                    node = h5file[entity["dlc:locator"]]
                    if entity["dlc:kind"] == "signal":
                        found = signal_fingerprint(node)
                    else:
                        found = image_fingerprint(node)
                    result["states"][entity["@id"]] = found == entity["dlc:fingerprint"]

    def roles(action: dict, key: str) -> list[str]:
        out = []
        for ref in action.get(key, []):
            binding = graph[ref["@id"]]
            target = (
                binding.get("dlc:state") or binding.get("dlc:artifact") or {}
            ).get("@id") or f"object {binding.get('dlc:objectUuid')}"
            out.append(f"{binding['name']}={target}")
        return out

    for ref in root.get("mentions", []):
        action = graph[ref["@id"]]
        result["activities"].append(
            {
                "id": action["@id"],
                "name": action.get("name"),
                "origin": action.get("dlc:origin"),
                "replayable": action.get("dlc:replayable"),
                "inputs": roles(action, "dlc:inputs"),
                "outputs": roles(action, "dlc:outputs"),
                "parameters": {
                    graph[p["@id"]]["name"]: json.loads(
                        graph[p["@id"]]["dlc:jsonValue"]
                    )
                    for p in action.get("dlc:parameters", [])
                },
            }
        )
    return result


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__)
        return 2
    result = check_capsule(*argv)
    print(f"Manifest fingerprint: {'ok' if result['manifest_ok'] else 'MISMATCH'}")
    if result["revision_of"]:
        print(f"Revision of: {result['revision_of']}")
    print(f"Workspace: {result['workspace']}")
    for state, ok in result["states"].items():
        print(f"  {state}: {'ok' if ok else 'ALTERED'}")
    for activity in result["activities"]:
        print(
            f"{activity['name']} [{activity['origin']}, "
            f"{'replayable' if activity['replayable'] else 'not replayable'}]"
        )
        print(f"  inputs: {', '.join(activity['inputs']) or '-'}")
        print(f"  outputs: {', '.join(activity['outputs']) or '-'}")
        if activity["parameters"]:
            print(f"  parameters: {json.dumps(activity['parameters'])}")
    good = (
        result["manifest_ok"]
        and result["workspace"] != "mismatch"
        and all(result["states"].values())
    )
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
