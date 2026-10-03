# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Packaged JSON Schemas and validation helper."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

__all__ = ["load_schema", "schema_errors"]


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Return a packaged JSON Schema (e.g. ``"ledger-1"``)."""
    path = resources.files("datalab_capsule").joinpath(f"schemas/{name}.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def schema_errors(name: str, data: Any) -> list[str]:
    """Return the JSON Schema errors of *data*, sorted by location."""
    import jsonschema  # pylint: disable=import-outside-toplevel

    validator = jsonschema.Draft202012Validator(load_schema(name))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in errors
    ]
