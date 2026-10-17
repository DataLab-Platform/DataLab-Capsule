# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Check that the format resources shipped by DataLab-Capsule match the profile.

Usage::

    python scripts/check_profile.py [--package SRC_OR_WHEEL] [--profile DIR]
        [--online URL] [--tag TAG]

Compares the bytes of the schemas and of the RO-Crate context carried by the
package (the ``src/datalab_capsule`` directory by default, or a built wheel)
with the SHA-256 table of the profile page (``index.html`` of the profile
directory, ``../capsule/0.1`` by default) and with the profile's own copies.
Lists the links from the profile to the reference implementation, and fails
when ``--tag`` is given and a link does not point to that tag.

Runs offline. ``--online URL`` (e.g. ``https://datalab-platform.com/capsule/0.1/``)
also downloads the published page and resources and compares them: this is a
separate delivery check, not a dependency of the ordinary tests.

Bytes are compared as they are: the published digests are those of the files
with LF line endings. A difference due only to line endings (e.g. a Windows
working copy checked out with CRLF) is reported as such, so that it is fixed in
the working copy instead of hidden by the check. Exits with 1 on any problem.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = (
    "schemas/ledger-1.schema.json",
    "schemas/report-1.schema.json",
    "schemas/review-1.schema.json",
    "contexts/ro-crate-1.3.jsonld",
)
IMPLEMENTATION = "https://github.com/DataLab-Platform/DataLab-Capsule/"
_ROW = re.compile(
    r'<a href="(?P<path>[^"]+)">.*?</td>.*?<code>(?P<sha>[0-9a-f]{64})</code>',
    re.DOTALL,
)
_LINK = re.compile(re.escape(IMPLEMENTATION) + r'(?:blob|tree)/(?P<ref>[^/"]+)/')


def sha256(data: bytes) -> str:
    """Return the hexadecimal SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def published_digests(index: str) -> dict[str, str]:
    """Return the resource digests listed in the table of the profile page."""
    digests = {}
    for row in index.split("<tr>")[1:]:
        match = _ROW.search(row)
        if match is not None and match["path"] in RESOURCES:
            digests[match["path"]] = match["sha"]
    return digests


def implementation_refs(index: str) -> list[str]:
    """Return the Git references of the links to the reference implementation."""
    return [match["ref"] for match in _LINK.finditer(index)]


def package_resources(package: Path) -> dict[str, bytes]:
    """Return the resources of a source directory or of a wheel."""
    if package.suffix == ".whl":
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            return {
                name: archive.read(f"datalab_capsule/{name}")
                for name in RESOURCES
                if f"datalab_capsule/{name}" in names
            }
    return {
        name: (package / name).read_bytes()
        for name in RESOURCES
        if (package / name).is_file()
    }


def _difference(name: str, data: bytes, expected: str, where: str) -> str | None:
    """Describe how *data* differs from the digest *expected*, or return None."""
    if sha256(data) == expected:
        return None
    if sha256(data.replace(b"\r\n", b"\n")) == expected:
        return f"{name} in {where}: same content with CRLF line endings"
    return f"{name} in {where}: SHA-256 {sha256(data)} instead of {expected}"


def compare(
    package: dict[str, bytes],
    index: str,
    profile: dict[str, bytes],
    tag: str | None = None,
) -> list[str]:
    """Return the problems found between the package and the profile.

    Args:
        package: Resources of the package, by relative path.
        index: Text of the profile page.
        profile: Copies of the resources held by the profile, by relative path.
        tag: Tag that every link to the implementation must point to, or None.

    Returns:
     The problems, empty when everything matches.
    """
    problems = []
    digests = published_digests(index)
    for name in RESOURCES:
        expected = digests.get(name)
        if expected is None:
            problems.append(f"{name}: no SHA-256 in the profile page")
            continue
        for where, files in (("package", package), ("profile", profile)):
            if name not in files:
                problems.append(f"{name}: missing from the {where}")
                continue
            problem = _difference(name, files[name], expected, where)
            if problem is not None:
                problems.append(problem)
    refs = implementation_refs(index)
    if tag is not None:
        problems.extend(
            f"implementation link points to {ref}, not to {tag}"
            for ref in refs
            if ref != tag
        )
    return problems


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def main() -> None:
    """Parse the arguments and run the checks."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--package", type=Path, default=ROOT / "src" / "datalab_capsule"
    )
    parser.add_argument("--profile", type=Path, default=ROOT.parent / "capsule" / "0.1")
    parser.add_argument("--online", metavar="URL")
    parser.add_argument("--tag")
    args = parser.parse_args()

    index = (args.profile / "index.html").read_text(encoding="utf-8")
    package = package_resources(args.package)
    profile = package_resources(args.profile)
    problems = compare(package, index, profile, args.tag)
    for name, digest in sorted(published_digests(index).items()):
        print(f"{digest}  {name}")
    print("implementation links:", ", ".join(implementation_refs(index)) or "none")
    if args.online:
        base = args.online.rstrip("/") + "/"
        online = {name: _fetch(base + name) for name in RESOURCES}
        problems.extend(
            f"online: {p}"
            for p in compare(package, _fetch(base).decode("utf-8"), online, args.tag)
        )
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        sys.exit(1)
    print(f"Profile resources OK ({args.package.name})")


if __name__ == "__main__":
    main()
