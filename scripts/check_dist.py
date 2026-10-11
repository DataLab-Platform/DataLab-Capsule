# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Check the built distributions of DataLab-Capsule before publishing them.

Usage::

    python scripts/check_dist.py DIST_DIR [--version X.Y.Z]

Checks that DIST_DIR holds exactly one wheel and one sdist of the same version
(equal to ``--version`` when given, e.g. the tag without its ``v``), that the
wheel carries the package data (schemas, RO-Crate context), the licence and the
two commands, declares the expected base requirements and the ``hdf5`` and
``registry`` extras, and holds no tests, and
that the sdist carries the sources, tests, corpus and documentation. Prints
what it checked and exits with 1 on the first problem.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

WHEEL_FILES = (
    "datalab_capsule/__init__.py",
    "datalab_capsule/cli.py",
    "datalab_capsule/registry.py",
    "datalab_capsule/registry_app.py",
    "datalab_capsule/registry_client.py",
    "datalab_capsule/schemas/ledger-1.schema.json",
    "datalab_capsule/schemas/report-1.schema.json",
    "datalab_capsule/schemas/review-1.schema.json",
    "datalab_capsule/contexts/ro-crate-1.3.jsonld",
)
SDIST_FILES = (
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "doc/scenario.md",
    "tests/fixtures/corpus/expected.json",
)
REQUIRES = {"jsonschema >= 4.17"}
EXTRAS = {
    'NumPy >= 1.22; extra == "hdf5"',
    'h5py >= 3.6; extra == "hdf5"',
    'fastapi >= 0.110; extra == "registry"',
    'uvicorn >= 0.27; extra == "registry"',
}
ENTRY_POINTS = (
    "workspace-capsule = datalab_capsule.cli:main",
    "workspace-capsule-registry = datalab_capsule.registry_app:main",
)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _normalise(requirement: str) -> str:
    return " ".join(requirement.replace(">=", " >= ").split()).replace("; ", "; ")


def check(dist: Path, version: str | None) -> None:
    """Run every check on the distributions found in *dist*."""
    wheels = sorted(dist.glob("datalab_capsule-*.whl"))
    sdists = sorted(dist.glob("datalab_capsule-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        _fail(f"expected one wheel and one sdist, found {wheels + sdists}")
    wheel, sdist = wheels[0], sdists[0]
    wheel_version = wheel.name.split("-")[1]
    sdist_version = sdist.name[len("datalab_capsule-") : -len(".tar.gz")]
    if wheel_version != sdist_version:
        _fail(f"wheel {wheel_version} and sdist {sdist_version} differ")
    if version is not None and wheel_version != version:
        _fail(f"distributions are {wheel_version}, expected {version}")
    print(f"version {wheel_version}: {wheel.name}, {sdist.name}")

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        info = f"datalab_capsule-{wheel_version}.dist-info"
        missing = [f for f in WHEEL_FILES if f not in names]
        if missing:
            _fail(f"wheel lacks {missing}")
        if not any(n.startswith(f"{info}/") and "LICENSE" in n for n in names):
            _fail("wheel lacks the licence file")
        if any(n.startswith("tests/") for n in names):
            _fail("wheel holds tests")
        metadata = Parser().parsestr(archive.read(f"{info}/METADATA").decode())
        requires = {_normalise(r) for r in metadata.get_all("Requires-Dist") or []}
        base = {r for r in requires if "extra ==" not in r}
        if base != {_normalise(r) for r in REQUIRES}:
            _fail(f"base requirements {sorted(base)} differ from {sorted(REQUIRES)}")
        missing = {_normalise(r) for r in EXTRAS} - requires
        if missing:
            _fail(f"extras lack {sorted(missing)}")
        entry_points = archive.read(f"{info}/entry_points.txt").decode()
        for line in ENTRY_POINTS:
            if line not in entry_points:
                _fail(f"wheel lacks the command {line!r}")
        if metadata["Name"] != "DataLab-Capsule":
            _fail(f"distribution name is {metadata['Name']!r}")
    print(f"wheel: {len(names)} files, package data, licence, commands, requirements")

    with tarfile.open(sdist) as archive:
        prefix = f"datalab_capsule-{sdist_version}/"
        names = {n[len(prefix) :] for n in archive.getnames() if n.startswith(prefix)}
        missing = [f for f in SDIST_FILES if f not in names]
        if missing:
            _fail(f"sdist lacks {missing}")
        corpus = [n for n in names if n.startswith("tests/fixtures/corpus/valid/")]
        if not corpus:
            _fail("sdist lacks the fixture corpus")
    print(f"sdist: {len(names)} files, sources, tests and {len(corpus)} valid fixtures")


def main() -> None:
    """Parse the arguments and run the checks."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    check(args.dist, args.version)
    print("Distributions OK")


if __name__ == "__main__":
    main()
