# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The format resources of the package match the SHA-256 table of the profile."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_profile.py"
PROFILE = ROOT.parent / "capsule" / "0.1"
if not SCRIPT.is_file():  # e.g. the test files alone, in Pyodide
    pytest.skip("scripts are not part of this test run", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("check_profile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_profile = _load()


def _index(files: dict[str, bytes], ref: str = "v0.1.0") -> str:
    rows = "".join(
        f'<tr><td><a href="{name}"><code>{name}</code></a></td><td>x</td>'
        f"<td><code>{check_profile.sha256(data)}</code></td></tr>\n"
        for name, data in files.items()
    )
    link = f'<a href="{check_profile.IMPLEMENTATION}blob/{ref}/doc/rules.md">'
    return f"<table>{rows}</table>{link}"


@pytest.fixture(name="files")
def fixture_files() -> dict[str, bytes]:
    return {
        name: f'{{"name": "{name}"}}\n'.encode() for name in check_profile.RESOURCES
    }


def test_identical_resources_pass(files):
    assert check_profile.compare(files, _index(files), dict(files), "v0.1.0") == []


def test_crlf_copy_is_reported_as_such(files):
    name = check_profile.RESOURCES[2]
    package = dict(files, **{name: files[name].replace(b"\n", b"\r\n")})
    problems = check_profile.compare(package, _index(files), dict(files))
    assert problems == [f"{name} in package: same content with CRLF line endings"]


def test_changed_and_missing_resources_fail(files):
    name = check_profile.RESOURCES[0]
    package = dict(files, **{name: b"{}"})
    del package[check_profile.RESOURCES[3]]
    problems = check_profile.compare(package, _index(files), dict(files))
    assert len(problems) == 2
    assert problems[0].startswith(f"{name} in package: SHA-256")
    assert problems[1] == f"{check_profile.RESOURCES[3]}: missing from the package"


def test_missing_digest_and_untagged_link_fail(files):
    index = _index({k: v for k, v in files.items() if "ledger" not in k}, ref="abc")
    problems = check_profile.compare(files, index, dict(files), "v0.1.0")
    assert problems == [
        "schemas/ledger-1.schema.json: no SHA-256 in the profile page",
        "implementation link points to abc, not to v0.1.0",
    ]


@pytest.mark.skipif(not PROFILE.is_dir(), reason="profile checkout not found")
def test_package_matches_the_profile_checkout():
    index = (PROFILE / "index.html").read_text(encoding="utf-8")
    package = check_profile.package_resources(ROOT / "src" / "datalab_capsule")
    profile = check_profile.package_resources(PROFILE)
    assert check_profile.compare(package, index, profile) == []
