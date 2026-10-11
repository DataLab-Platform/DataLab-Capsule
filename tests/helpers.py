# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Shared test helpers."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np


def signal(y, x=None, *, xunit: str = "s", yunit: str = "", roi=None, dy=None):
    """Return a duck-typed signal object (as Sigima's ``SignalObj`` exposes)."""
    y = np.asarray(y, dtype=np.float64)
    if x is None:
        x = np.array([0.0, 0.25, 0.5, 0.75])[: y.size]
    return SimpleNamespace(
        x=np.asarray(x, dtype=np.float64),
        y=y,
        dx=None,
        dy=dy,
        xunit=xunit,
        yunit=yunit,
        roi=roi,
    )


class ROI:
    """Duck-typed region of interest (as Sigima's ROI classes expose)."""

    def __init__(self, coords, kind: str = "SegmentROI") -> None:
        self.coords = np.asarray(coords, dtype=np.float64)
        self.kind = kind

    def to_dict(self) -> dict:
        """Return the ROI definition (keys of Sigima's ``ROI.to_dict``)."""
        single = {"coords": self.coords, "indices": False, "title": ""}
        single["type"] = self.kind
        if self.kind != "SegmentROI":
            single["inverse"] = False
        return {"single_rois": [single]}


def write_metadata(group, name: str, value) -> None:
    """Write *value* under *group* with guidata's HDF5 dict/sequence layout."""
    if isinstance(value, dict):
        node = group.create_group(name)
        node.attrs["__dict"] = len(value)
        for key, item in value.items():
            if isinstance(item, (dict, list, np.ndarray)):
                write_metadata(node, key, item)
            else:
                node.attrs[key] = item
    elif isinstance(value, list):
        node = group.create_group(name)
        node.attrs["__seq"] = len(value)
        for index, item in enumerate(value):
            write_metadata(node, f"__seq{index}", item)
    else:
        group[name] = value


def image(data, *, x0=0.0, y0=0.0, dx=1.0, dy=1.0, coords=None, roi=None):
    """Return a duck-typed image object (as Sigima's ``ImageObj`` exposes)."""
    xcoords, ycoords = (None, None) if coords is None else coords
    return SimpleNamespace(
        data=np.asarray(data),
        is_uniform_coords=coords is None,
        x0=x0,
        y0=y0,
        dx=dx,
        dy=dy,
        xcoords=xcoords,
        ycoords=ycoords,
        xunit="mm",
        yunit="mm",
        zunit="counts",
        roi=roi,
    )
