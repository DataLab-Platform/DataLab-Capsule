# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Measure the time and memory cost of state fingerprints.

Run in CPython::

    python benchmarks/fingerprint_cost.py [--profile quick|representative|stress]
        [--repeat N] [--output PATH] [--case ID]...

Run in Pyodide (Node): ``node tests/pyodide/run_benchmark.mjs <wheel>
fingerprint_cost.py [same options]``.

Each case measures :func:`datalab_capsule.integrity.state_facts`, the function
used by the recorders of both editions, on one signal or image. A case whose
id ends with ``.sha256`` measures a plain SHA-256 of the same arrays: a lower
bound for comparison, not a substitute for the scientific fingerprint (which
also covers dtype, shape, coordinates, units and ROI). ``roi-*`` cases measure
the ROI definition alone. Preparing the arrays is never timed. Every case first
checks that the fingerprint equals the one of a contiguous little-endian copy of
the same content: non-contiguous or big-endian inputs must not change it.

Profiles: ``quick`` (small images and signals, ROI), ``representative`` (2048²
images, 1 M point signals of several dtypes, non-contiguous and big-endian
images), ``stress`` (adds a 4096² float64 image).
"""

from __future__ import annotations

import hashlib
import os.path as osp
import sys
from types import SimpleNamespace
from typing import Any

import numpy as np

sys.path.insert(0, osp.dirname(osp.abspath(__file__)))

from _harness import Case, main  # noqa: E402

from datalab_capsule.integrity import roi_facts, state_facts  # noqa: E402

QUICK, REPR, STRESS = "quick", "representative", "stress"
FROM_REPR = (REPR, STRESS)
ALL = (QUICK, REPR, STRESS)


class Roi:
    """ROI exposing ``to_dict()``, as Sigima's ROI classes do."""

    def __init__(self, count: int, kind: str) -> None:
        self.definition = {
            "single_rois": [
                {
                    "type": "rectangle" if kind == "image" else "segment",
                    "title": f"ROI {index}",
                    "coords": [float(index), 1.0, 10.0, 20.0]
                    if kind == "image"
                    else [float(index), float(index) + 0.5],
                    "inverse": False,
                }
                for index in range(count)
            ]
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the ROI definition."""
        return self.definition


def _image(rows: int, cols: int, dtype: str, layout: str, roi: int) -> Any:
    rng = np.random.default_rng(0)
    data = (rng.random((rows, cols)) * 1000).astype(dtype)
    if layout == "transposed":
        data = np.ascontiguousarray(data.T).T
    elif layout == "big-endian":
        data = data.astype(data.dtype.newbyteorder(">"))
    return SimpleNamespace(
        data=data,
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
        roi=Roi(roi, "image") if roi else None,
    )


def _signal(points: int, dtype: str, roi: int) -> Any:
    rng = np.random.default_rng(0)
    x = np.linspace(0.0, 1.0, points)
    y = rng.random(points)
    if np.dtype(dtype).kind == "c":
        y = y + 1j * rng.random(points)
    return SimpleNamespace(
        x=x,
        y=y.astype(dtype),
        dx=None,
        dy=None,
        xunit="s",
        yunit="V",
        roi=Roi(roi, "signal") if roi else None,
    )


def _arrays(obj: Any) -> list[np.ndarray]:
    return [obj.data] if hasattr(obj, "data") else [obj.x, obj.y]


def _canonical(obj: Any) -> Any:
    """Return a copy of *obj* with contiguous little-endian arrays."""
    copy = SimpleNamespace(**vars(obj))
    for name in ("data", "x", "y"):
        if hasattr(copy, name):
            array = getattr(copy, name)
            little = array.dtype.newbyteorder("<")
            setattr(copy, name, np.ascontiguousarray(array, little))
    return copy


def _check_facts(obj: Any, facts: dict[str, Any]) -> None:
    expected = state_facts(_canonical(obj))["fingerprint"]
    assert facts["fingerprint"] == expected, "fingerprint differs from canonical copy"


def _sha256(obj: Any) -> str:
    hasher = hashlib.sha256()
    for array in _arrays(obj):
        hasher.update(memoryview(np.ascontiguousarray(array)).cast("B"))
    return hasher.hexdigest()


def _sizes(obj: Any) -> dict[str, Any]:
    arrays = _arrays(obj)
    sizes: dict[str, Any] = {"array_bytes": sum(int(a.nbytes) for a in arrays)}
    if hasattr(obj, "data"):
        sizes["shape"] = list(obj.data.shape)
        sizes["contiguous"] = bool(obj.data.flags.c_contiguous)
    else:
        sizes["points"] = int(obj.y.shape[0])
    sizes["roi_regions"] = (
        0 if obj.roi is None else len(obj.roi.definition["single_rois"])
    )
    return sizes


def _pair(
    case_id: str, profiles: tuple[str, ...], params: dict[str, Any], make: Any
) -> list[Case]:
    """Return the fingerprint case and its SHA-256 lower bound, sharing data."""
    return [
        Case(
            id=case_id,
            profiles=profiles,
            params={**params, "method": "state_facts"},
            setup=make,
            run=state_facts,
            sizes=_sizes,
            check=_check_facts,
            group=case_id,
        ),
        Case(
            id=f"{case_id}.sha256",
            profiles=profiles,
            params={**params, "method": "sha256 (lower bound)"},
            setup=make,
            run=_sha256,
            sizes=_sizes,
            group=case_id,
        ),
    ]


def cases(_profile: str) -> list[Case]:
    """Return every case; the harness keeps those of the profile."""
    result: list[Case] = []
    images = (
        (512, "uint16", "contiguous", 0, ALL),
        (512, "float64", "contiguous", 0, ALL),
        (512, "float64", "contiguous", 1, ALL),
        (2048, "uint16", "contiguous", 0, FROM_REPR),
        (2048, "float64", "contiguous", 0, FROM_REPR),
        (2048, "float64", "transposed", 0, FROM_REPR),
        (2048, "float64", "big-endian", 0, FROM_REPR),
        (4096, "float64", "contiguous", 0, (STRESS,)),
    )
    for size, dtype, layout, roi, profiles in images:
        case_id = f"image-{size}-{dtype}"
        case_id += "" if layout == "contiguous" else f"-{layout}"
        case_id += "-roi" if roi else ""
        params = {"kind": "image", "shape": [size, size], "dtype": dtype}
        params.update(layout=layout, roi_regions=roi)
        result += _pair(
            case_id,
            profiles,
            params,
            lambda s=size, d=dtype, la=layout, r=roi: _image(s, s, d, la, r),
        )
    signals = (
        (100_000, "float64", 0, ALL),
        (100_000, "float64", 1, ALL),
        (1_000_000, "float32", 0, FROM_REPR),
        (1_000_000, "float64", 0, FROM_REPR),
        (1_000_000, "complex128", 0, FROM_REPR),
    )
    for points, dtype, roi, profiles in signals:
        label = f"{points // 1000}k" if points < 1_000_000 else f"{points // 10**6}M"
        case_id = f"signal-{label}-{dtype}" + ("-roi" if roi else "")
        params = {"kind": "signal", "points": points, "x_dtype": "float64"}
        params.update(y_dtype=dtype, roi_regions=roi)
        result += _pair(
            case_id,
            profiles,
            params,
            lambda p=points, d=dtype, r=roi: _signal(p, d, r),
        )
    for count in (1, 100):
        result.append(
            Case(
                id=f"roi-definition-{count}",
                profiles=ALL,
                params={"kind": "roi", "regions": count, "method": "roi_facts"},
                setup=lambda c=count: Roi(c, "image"),
                run=roi_facts,
                sizes=lambda roi: {"roi_regions": len(roi.definition["single_rois"])},
            )
        )
    return result


if __name__ == "__main__":
    main(__file__, cases, __doc__.splitlines()[0])
