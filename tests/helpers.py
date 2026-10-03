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
