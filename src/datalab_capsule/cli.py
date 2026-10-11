# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""``workspace-capsule`` command line: validate, inspect, compare and verify capsules.

Usage::

    workspace-capsule validate CAPSULE
    workspace-capsule inspect CAPSULE [--json]
    workspace-capsule diff OLD NEW [--json]
    workspace-capsule verify CAPSULE [--workspace FILE] [--report FILE]...
                             [--review FILE]... [--json]

``diff`` exits with 0 when the capsules are identical, 1 when they differ.
``verify`` exits with 0 when no problem is found, 1 otherwise. Errors exit with
2 (``diff``) or 1. Nothing is ever computed or fetched: a referenced workspace
is checked only when its file is given with ``--workspace``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from datalab_capsule.archive import read_capsule
from datalab_capsule.diff import diff_manifests
from datalab_capsule.integrity import ProvenanceError, load_json
from datalab_capsule.manifest import inspect_manifest
from datalab_capsule.verify import verify_capsule

__all__ = ["main"]


def _short(ref: str | None) -> str:
    return "-" if ref is None else ref.split("-", 1)[-1][:8]


def _print_summary(summary: dict) -> None:
    print(f"Capsule: {summary['name']} ({summary['date_published']})")
    print(f"Profiles: {', '.join(summary['profiles'])}")
    print(f"Fingerprint: {summary['fingerprint']}")
    workspace = summary["workspace"]
    where = (
        f"referenced at {workspace['id']}" if workspace["referenced"] else "included"
    )
    print(
        f"Workspace ({where}): {workspace['size']} bytes, sha256 {workspace['sha256']}"
    )
    print(f"Activities ({len(summary['activities'])}):")
    for index, act in enumerate(summary["activities"], 1):
        status = "replayable" if act["replayable"] else "opaque"
        print(f"  {index}. {act['name']} [{status}, {act['origin']}]")
        print(f"     parameters: {json.dumps(act['parameters'], sort_keys=True)}")
        for direction in ("inputs", "outputs"):
            roles = ", ".join(
                f"{item['position']}:{item['role']}={_short(item['target'])}"
                for item in act[direction]
            )
            print(f"     {direction}: {roles}")
    print(f"States ({len(summary['states'])}):")
    for state in summary["states"]:
        located = state["locator"] or "not in workspace"
        consumers = ", ".join(_short(c) for c in state["consumed_by"]) or "-"
        print(
            f"  {_short(state['id'])}: {located}; produced by "
            f"{_short(state['produced_by'])}; consumed by {consumers}"
        )
    if summary["fan_out"]:
        print(f"Fan-out: {', '.join(_short(s) for s in summary['fan_out'])}")


def _print_diff(diff: dict) -> None:
    print(f"Old: {diff['old']}")
    print(f"New: {diff['new']}")
    if diff["revision_of_old"]:
        print("New is a revision of old.")
    if diff["identical"]:
        print("Identical.")
        return
    for group in ("scientific", "presentation", "environment"):
        changes = diff[group]
        print(f"{group.capitalize()} changes ({len(changes)}):")
        for change in changes:
            line = f"  {change['change']} {change['item']} {_short(change['id'])}"
            if "field" in change:
                line += f" {change['field']}: {change['old']!r} -> {change['new']!r}"
            print(line)


def _print_verification(result: dict) -> None:
    print(f"Capsule: {result['fingerprint']}")
    print(f"Workspace: {result['workspace']}")
    if result["states_located"] is not None:
        print(
            f"Stored states: {result['states_located']}, "
            f"altered: {len(result['states_altered'])}"
        )
    for entry in result["reports"]:
        print(f"Report {entry['activity_id']} ({entry['verdict']}): {entry['status']}")
    for entry in result["reviews"]:
        print(f"Review {entry['fingerprint']}: {entry['status']}")
    for problem in result["problems"]:
        print(f"Problem: {problem}")
    print("Verified." if result["valid"] else "Not verified.")


def _load_json(path: str) -> object:
    return load_json(Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = argparse.ArgumentParser(
        prog="workspace-capsule",
        description="Validate, inspect, compare and verify DataLab workspace capsules.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a capsule")
    validate.add_argument("capsule")
    inspect = commands.add_parser("inspect", help="describe a capsule")
    inspect.add_argument("capsule")
    inspect.add_argument("--json", action="store_true", help="JSON output")
    diff = commands.add_parser("diff", help="compare two capsules")
    diff.add_argument("old")
    diff.add_argument("new")
    diff.add_argument("--json", action="store_true", help="JSON output")
    verify = commands.add_parser("verify", help="verify a capsule (no computation)")
    verify.add_argument("capsule")
    verify.add_argument("--workspace", help="file of a referenced workspace")
    verify.add_argument("--report", action="append", default=[], help="report file")
    verify.add_argument("--review", action="append", default=[], help="review file")
    verify.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)
    if args.command == "verify":
        return _verify(args)
    if args.command == "diff":
        try:
            result = diff_manifests(
                read_capsule(args.old).manifest, read_capsule(args.new).manifest
            )
        except (OSError, ProvenanceError) as exc:
            print(f"Invalid capsule: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            _print_diff(result)
        return 0 if result["identical"] else 1
    try:
        capsule = read_capsule(args.capsule)
    except (OSError, ProvenanceError) as exc:
        print(f"Invalid capsule: {exc}", file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"Valid capsule: {args.capsule}")
        return 0
    summary = inspect_manifest(capsule.manifest)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_summary(summary)
    return 0


def _verify(args: argparse.Namespace) -> int:
    resolver = None
    if args.workspace:
        workspace = Path(args.workspace).read_bytes()
        resolver = lambda _iri: workspace  # noqa: E731
    try:
        reports = [_load_json(path) for path in args.report]
        reviews = [_load_json(path) for path in args.review]
    except (OSError, ValueError) as exc:
        print(f"Unreadable document: {exc}", file=sys.stderr)
        return 1
    result = verify_capsule(
        args.capsule, resolve_workspace=resolver, reports=reports, reviews=reviews
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_verification(result)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
