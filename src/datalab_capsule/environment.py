# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Execution environment records shared by DataLab Desktop and DataLab-Web.

A record never includes host or user names. Its identifier is the SHA-256 of its
RFC 8785 canonical JSON form, so identical environments share one entry.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterable
from importlib import metadata
from typing import Any

from datalab_capsule.integrity import json_digest

__all__ = [
    "DEFAULT_PACKAGES",
    "EDITIONS",
    "collect_environment",
    "environment_id",
]

EDITIONS = ("desktop", "web")
DEFAULT_PACKAGES = ("sigima", "guidata", "numpy", "scipy", "h5py")


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _pyodide_version() -> str | None:
    if sys.platform != "emscripten":
        return None
    try:
        import pyodide  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    return getattr(pyodide, "__version__", None)


def collect_environment(
    edition: str,
    edition_version: str,
    edition_revision: str | None = None,
    packages: Iterable[str] = DEFAULT_PACKAGES,
) -> dict[str, Any]:
    """Collect the environment record of the running interpreter.

    Args:
        edition: ``"desktop"`` or ``"web"``.
        edition_version: Application version.
        edition_revision: Development commit, or ``None``.
        packages: Distribution names whose versions are recorded.

    Returns:
        JSON-compatible environment record.

    Raises:
        ValueError: If *edition* is unknown.
    """
    if edition not in EDITIONS:
        raise ValueError(f"Unknown edition: {edition!r}")
    return {
        "edition": edition,
        "edition_version": str(edition_version),
        "edition_revision": edition_revision,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": sys.platform,
        "machine": platform.machine(),
        "pyodide": _pyodide_version(),
        "packages": {name: _package_version(name) for name in packages},
    }


def environment_id(record: dict[str, Any]) -> str:
    """Return the identifier of an environment record."""
    return json_digest(record)
