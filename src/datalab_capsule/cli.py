# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""``workspace-capsule`` command line: validate and inspect capsules.

Usage::

    workspace-capsule validate CAPSULE
    workspace-capsule inspect CAPSULE [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from datalab_capsule.archive import read_capsule
from datalab_capsule.integrity import ProvenanceError
from datalab_capsule.manifest import inspect_manifest

__all__ = ["main"]


def _short(ref: str | None) -> str:
    return "-" if ref is None else ref.split("-", 1)[-1][:8]


def _print_summary(summary: dict) -> None:
    print(f"Capsule: {summary['name']} ({summary['date_published']})")
    print(f"Profiles: {', '.join(summary['profiles'])}")
    print(f"Fingerprint: {summary['fingerprint']}")
    workspace = summary["workspace"]
    print(f"Workspace: {workspace['size']} bytes, sha256 {workspace['sha256']}")
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = argparse.ArgumentParser(
        prog="workspace-capsule",
        description="Validate and inspect DataLab workspace capsules.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a capsule")
    validate.add_argument("capsule")
    inspect = commands.add_parser("inspect", help="describe a capsule")
    inspect.add_argument("capsule")
    inspect.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
