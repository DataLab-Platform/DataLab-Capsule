# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for environment records."""

from __future__ import annotations

import getpass
import json
import platform

import pytest

from datalab_capsule.environment import collect_environment, environment_id


def test_environment_record() -> None:
    """The record has the documented fields and a stable identifier."""
    record = collect_environment("web", "0.9.0", packages=("numpy", "missing-pkg"))
    assert set(record) == {
        "edition",
        "edition_version",
        "edition_revision",
        "python",
        "platform",
        "machine",
        "pyodide",
        "packages",
    }
    assert record["packages"]["missing-pkg"] is None
    assert record["packages"]["numpy"]
    assert environment_id(record) == environment_id(json.loads(json.dumps(record)))
    assert environment_id(record).startswith("sha256:")
    other = collect_environment("desktop", "0.9.0", packages=("numpy", "missing-pkg"))
    assert environment_id(other) != environment_id(record)


def test_environment_has_no_host_or_user() -> None:
    """Host and user names never appear in the record."""
    text = json.dumps(collect_environment("desktop", "1.3.0"))
    assert platform.node() not in text
    assert getpass.getuser() not in text


def test_unknown_edition() -> None:
    """Only the two editions are accepted."""
    with pytest.raises(ValueError):
        collect_environment("kernel", "1.0")
