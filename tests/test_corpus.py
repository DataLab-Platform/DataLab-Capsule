# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Exit gate of the format and toolkit: the command line on the fixture corpus.

Every case of ``fixtures/corpus/expected.json`` (written by hand) runs through the
command line with the network disabled and no application imported. The same
module runs in CPython and in Pyodide (``tests/pyodide/run_pyodide.mjs``).
"""

from __future__ import annotations

import collections
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from datalab_capsule.cli import main

from .corpus import CORPUS, build

CASES = json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))["cases"]
APPLICATIONS = ("datalab", "sigima", "sigimax", "guidata", "plotpy", "qtpy")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to open a connection or resolve a name."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Network access attempted")

    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse, raising=False)


def _check(expected: Any, actual: Any, where: str = "") -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            if key == "changes":
                for group, changes in value.items():
                    found = sorted(
                        " ".join(
                            str(c[k]) for k in ("change", "item", "field") if k in c
                        )
                        for c in actual[group]
                    )
                    assert found == sorted(changes), f"{where}{group}"
                continue
            _check(value, actual[key], f"{where}{key}.")
    elif isinstance(expected, list) and isinstance(actual, list):
        assert len(expected) == len(actual), where
        for index, (exp, act) in enumerate(zip(expected, actual)):
            _check(exp, act, f"{where}{index}.")
    elif (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and isinstance(actual, list)
    ):
        assert len(actual) == expected, where
    else:
        assert actual == expected, where


def _case_id(case: dict) -> str:
    return " ".join(Path(a).stem if "/" in a else a for a in case["argv"])


@pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
def test_cli_on_corpus(
    case: dict, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command line gives the expected result on each corpus case."""
    monkeypatch.chdir(CORPUS)
    code = main(case["argv"])
    out = capsys.readouterr().out
    assert code == case["exit"]
    if "json" in case:
        _check(case["json"], json.loads(out))


def test_every_fixture_is_exercised() -> None:
    """Each capsule and document of the corpus appears in at least one case."""
    used = collections.Counter(a for case in CASES for a in case["argv"])
    files = [
        p.relative_to(CORPUS).as_posix()
        for folder in ("valid", "invalid")
        for p in (CORPUS / folder).iterdir()
    ]
    assert files and [f for f in files if not used[f]] == []


def test_corpus_is_up_to_date() -> None:
    """The committed corpus matches a fresh build from its sources."""
    built = build()
    on_disk = {
        p.relative_to(CORPUS).as_posix()
        for folder in ("valid", "invalid")
        for p in (CORPUS / folder).iterdir()
    }
    assert set(built) == on_disk
    for path, data in built.items():
        assert (CORPUS / path).read_bytes() == data, path


def test_no_application_is_imported() -> None:
    """The toolkit runs without DataLab, Sigima or any GUI library."""
    loaded = {name.split(".")[0] for name in sys.modules}
    assert loaded.isdisjoint(APPLICATIONS)
