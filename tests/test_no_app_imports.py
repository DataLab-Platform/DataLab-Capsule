# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The package never imports the applications or Qt."""

from __future__ import annotations

import re
from pathlib import Path

import datalab_capsule

FORBIDDEN = re.compile(
    r"^\s*(?:import|from)\s+(datalab|sigima|sigimax|guidata|plotpy|qtpy|PyQt\d|PySide\d)\b",
    re.MULTILINE,
)


def test_no_application_imports() -> None:
    """No module of the package imports DataLab, Sigima, SigimaX, guidata or Qt."""
    root = Path(datalab_capsule.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if FORBIDDEN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
