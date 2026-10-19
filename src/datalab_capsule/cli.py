# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""``workspace-capsule`` command line: create, check, compare, review and exchange.

Usage::

    workspace-capsule validate CAPSULE
    workspace-capsule inspect CAPSULE [--json]
    workspace-capsule diff OLD NEW [--json]
    workspace-capsule verify CAPSULE [--workspace FILE] [--report FILE]...
                             [--review FILE]... [--json]
    workspace-capsule attachment CAPSULE NAME OUTPUT
    workspace-capsule workspace CAPSULE OUTPUT
    workspace-capsule create WORKSPACE OUTPUT [--name NAME] [--description TEXT]
                             [--revision-of CAPSULE] [--workspace-ref IRI]
                             [--attach FILE]...
    workspace-capsule review CAPSULE OUTPUT --reviewer NAME --statement TEXT
                             [--verdict VERDICT] [--report FILE]...
    workspace-capsule registry list URL [--json]
    workspace-capsule registry publish URL CAPSULE [--review FILE]...
                             [--token-env VARIABLE]
    workspace-capsule registry fetch URL FINGERPRINT FOLDER

``diff`` compares capsules or manifest files (``.json``, e.g. fetched from a
registry) and exits with 0 when they are identical, 1 when they differ.
``verify`` exits with 0 when no problem is found, 1 otherwise. Errors exit with
2 (``diff``) or 1. Nothing is ever computed. Only the ``registry`` commands use
the network, at the address given; everything they fetch is checked. A
referenced workspace is checked only when its file is given with
``--workspace``. ``attachment`` writes one checked attachment to OUTPUT and
``workspace`` the checked workspace (to open in DataLab); no
command overwrites a file or opens an attachment. ``registry fetch`` does not
save an attachment whose name Windows reserves (``NUL``, ``CON``...). ``create``
needs h5py.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import mimetypes
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from datalab_capsule.archive import create_from_hdf5, read_capsule
from datalab_capsule.diff import diff_manifests
from datalab_capsule.integrity import ProvenanceError, load_json
from datalab_capsule.manifest import (
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.registry_client import RegistryClient, fetch_revision
from datalab_capsule.review import VERDICTS, create_review
from datalab_capsule.verify import verify_capsule

__all__ = ["main", "registry_main"]


def _short(ref: str | None) -> str:
    if ref is None:
        return "-"
    if not ref.startswith("#"):
        return ref
    ident = ref.split("-", 1)[-1]
    return ident[:19] if ident.startswith("sha256:") else ident[:8]


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
    for item in summary["attachments"]:
        print(
            f"Attachment {item['name']} ({item['media_type']}, "
            f"{item['size']} bytes): {item['description'] or '-'}"
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


_CONCLUSIONS = {
    "differences": "scientific changes found",
    "none": "no scientific change, same workspace file and attachments",
    "none_detected": (
        "no change in the recorded activities and states; the workspace files "
        "or the attachments differ and content outside the recorded provenance "
        "is not compared"
    ),
}


def _print_diff(diff: dict) -> None:
    print(f"Old: {diff['old']}")
    print(f"New: {diff['new']}")
    if diff["revision_of_old"]:
        print("New is a revision of old.")
    if diff["identical"]:
        print("Identical.")
        return
    print(f"Scientific conclusion: {_CONCLUSIONS[diff['scientific_conclusion']]}")
    groups = ("scientific", "presentation", "storage", "environment", "attachments")
    for group in groups:
        changes = diff[group]
        label = "Attachment" if group == "attachments" else group.capitalize()
        print(f"{label} changes ({len(changes)}):")
        for change in changes:
            ident = change["id"]
            if change["item"] != "attachment":
                ident = _short(ident)
            line = f"  {change['change']} {change['item']} {ident}"
            if "field" in change:
                line += f" {change['field']}: {change['old']!r} -> {change['new']!r}"
            print(line)
    if not any(diff[group] for group in groups):
        print("The manifests differ outside these groups (e.g. the parent revision).")


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
    if isinstance(sys.stdout, io.TextIOWrapper):
        # Names and units may hold any character, whatever the console or pipe
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
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
    attachment = commands.add_parser(
        "attachment", help="write one attachment to a file (never opened)"
    )
    attachment.add_argument("capsule")
    attachment.add_argument("name")
    attachment.add_argument("output")
    extract = commands.add_parser(
        "workspace", help="write the checked workspace of a capsule to a file"
    )
    extract.add_argument("capsule")
    extract.add_argument("output")
    create = commands.add_parser(
        "create", help="create a capsule from a saved workspace (needs h5py)"
    )
    create.add_argument("workspace")
    create.add_argument("output")
    create.add_argument("--name")
    create.add_argument("--description")
    create.add_argument("--revision-of", help="parent capsule (file)")
    create.add_argument("--workspace-ref", help="absolute IRI of the workspace")
    create.add_argument("--attach", action="append", default=[], help="light file")
    review = commands.add_parser("review", help="write a review of a capsule")
    review.add_argument("capsule")
    review.add_argument("output")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reviewer-id")
    review.add_argument("--statement", required=True)
    review.add_argument("--verdict", choices=VERDICTS)
    review.add_argument("--report", action="append", default=[], help="report file")
    registry = commands.add_parser("registry", help="use a capsule registry")
    actions = registry.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="list the revisions")
    listing.add_argument("url")
    listing.add_argument("--json", action="store_true", help="JSON output")
    publish = actions.add_parser(
        "publish", help="publish a manifest, its attachments and reviews"
    )
    publish.add_argument("url")
    publish.add_argument("capsule")
    publish.add_argument("--review", action="append", default=[], help="review file")
    publish.add_argument(
        "--token-env", metavar="VARIABLE", help="variable holding the token"
    )
    fetch = actions.add_parser(
        "fetch", help="fetch and check a manifest, its reviews and attachments"
    )
    fetch.add_argument("url")
    fetch.add_argument("fingerprint")
    fetch.add_argument("folder", help="new folder for the fetched files")
    args = parser.parse_args(argv)
    if args.command == "verify":
        return _verify(args)
    if args.command in ("create", "review", "registry"):
        try:
            return {"create": _create, "review": _review, "registry": _registry}[
                args.command
            ](args)
        except (OSError, ValueError, ProvenanceError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    if args.command == "diff":
        try:
            result = diff_manifests(_manifest(args.old), _manifest(args.new))
        except (OSError, ValueError, ProvenanceError) as exc:
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
    if args.command == "attachment":
        if args.name not in capsule.attachments:
            print(f"No attachment named {args.name!r}", file=sys.stderr)
            return 1
        try:
            with open(args.output, "xb") as file:
                file.write(capsule.attachments[args.name])
        except OSError as exc:
            print(f"Cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote {args.name} to {args.output}")
        return 0
    if args.command == "workspace":
        if capsule.workspace is None:
            print(
                f"The workspace is referenced, not included: {capsule.workspace_ref}",
                file=sys.stderr,
            )
            return 1
        try:
            _write_new(Path(args.output), capsule.workspace)
        except OSError as exc:
            print(f"Cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote the checked workspace to {args.output}")
        return 0
    summary = inspect_manifest(capsule.manifest)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_summary(summary)
    return 0


def _verify(args: argparse.Namespace) -> int:
    resolver = None
    try:
        if args.workspace:
            workspace = Path(args.workspace).read_bytes()
            resolver = lambda _iri: workspace  # noqa: E731
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


def _manifest(path: str) -> Any:
    """Manifest of a capsule, or of a manifest file (validated)."""
    if path.lower().endswith(".json"):
        manifest = load_json(Path(path).read_bytes())
        validate_manifest(manifest)
        return manifest
    return read_capsule(path).manifest


def _write_new(path: Path, data: bytes) -> None:
    """Write *data* to a new file; never overwrite."""
    with open(path, "xb") as file:
        file.write(data)


#: Device names Windows resolves in any folder and with any extension.
_WINDOWS_DEVICES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"{port}{digit}" for port in ("COM", "LPT") for digit in range(10)]
)


def _portable_file_name(name: str) -> bool:
    """Return False for a name Windows would not create as that regular file.

    Writing to a device name would discard the data (``NUL``) or print it
    (``CON``); Windows also drops a trailing dot.
    """
    return name.split(".", 1)[0].upper() not in _WINDOWS_DEVICES and not name.endswith(
        "."
    )


def _create(args: argparse.Namespace) -> int:
    try:
        import h5py  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
    except ImportError:
        print('create needs h5py: pip install "datalab-capsule[hdf5]"', file=sys.stderr)
        return 1
    attachments = []
    for name in args.attach:
        path = Path(name)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attachments.append(
            {"name": path.name, "data": path.read_bytes(), "media_type": media_type}
        )
    parent = None
    if args.revision_of:
        parent = manifest_fingerprint(read_capsule(args.revision_of).manifest)
    capsule = create_from_hdf5(
        Path(args.workspace),
        name=args.name,
        description=args.description,
        revision_of=parent,
        workspace_ref=args.workspace_ref,
        attachments=attachments,
    )
    _write_new(Path(args.output), capsule)
    summary = inspect_manifest(read_capsule(capsule).manifest)
    print(f"Wrote {args.output}: {summary['fingerprint']}")
    print(
        f"{len(summary['activities'])} activities, {len(summary['states'])} states, "
        f"{len(summary['attachments'])} attachments"
    )
    return 0


def _review(args: argparse.Namespace) -> int:
    manifest = read_capsule(args.capsule).manifest
    review = create_review(
        manifest,
        reviewer=args.reviewer,
        reviewer_id=args.reviewer_id,
        statement=args.statement,
        verdict=args.verdict,
        reports=[_load_json(path) for path in args.report],
    )
    text = json.dumps(review, indent=2, ensure_ascii=False) + "\n"
    _write_new(Path(args.output), text.encode("utf-8"))
    print(f"Wrote {args.output}: {review['review_fingerprint']}")
    return 0


def _registry(args: argparse.Namespace) -> int:
    token = None
    if getattr(args, "token_env", None):
        token = os.environ.get(args.token_env)
        if not token:
            print(f"Environment variable {args.token_env} is empty", file=sys.stderr)
            return 1
    client = RegistryClient(args.url, token)
    if args.action == "list":
        revisions = client.list_revisions()
        if args.json:
            print(json.dumps(revisions, indent=2, ensure_ascii=False))
        for revision in [] if args.json else revisions:
            print(
                f"{revision['fingerprint']}  {revision['name']}  "
                f"parent: {revision['revision_of'] or '-'}  "
                f"attachments: {len(revision['attachments'])}  "
                f"reviews: {revision['reviews']}"
            )
        return 0
    if args.action == "publish":
        reviews = [Path(path).read_bytes() for path in args.review]
        result = client.publish_capsule(args.capsule, reviews)
        state = "published" if result["created"] else "already published"
        print(
            f"{result['fingerprint']} {state}; {result['attachments']} attachments "
            f"and {result['reviews']} reviews sent (never the workspace)"
        )
        return 0
    fetched = fetch_revision(client, args.fingerprint)
    folder = Path(args.folder)
    folder.mkdir()
    text = json.dumps(fetched["manifest"], indent=2, ensure_ascii=False) + "\n"
    _write_new(folder / "manifest.json", text.encode("utf-8"))
    print(f"Manifest checked: {args.fingerprint}")
    problems = 0
    for index, entry in enumerate(fetched["reviews"], start=1):
        review = entry["review"]
        if entry["status"] == "valid":
            body = json.dumps(review, indent=2, ensure_ascii=False) + "\n"
            _write_new(folder / f"review-{index}.json", body.encode("utf-8"))
            print(
                f"Review {index}: valid, {review['reviewer']['name']}, "
                f"{review['verdict'] or 'no verdict'}"
            )
        else:
            problems += 1
            print(f"Review {index}: {entry['status']}")
    for entry in fetched["attachments"]:
        if entry["data"] is None:
            problems += 1
            print(f"Attachment {entry['name']}: {entry['status']}")
            continue
        if not _portable_file_name(entry["name"]):
            problems += 1
            print(
                f"Attachment {entry['name']}: checked, not saved "
                "(Windows does not accept this file name)"
            )
            continue
        (folder / "attachments").mkdir(exist_ok=True)
        _write_new(folder / "attachments" / entry["name"], entry["data"])
        print(f"Attachment {entry['name']}: checked, saved (not opened)")
    return 0 if problems == 0 else 1


def registry_main(argv: Sequence[str] | None = None) -> int:
    """Run the ``workspace-capsule-registry`` server, if its extra is installed."""
    if any(importlib.util.find_spec(name) is None for name in ("fastapi", "uvicorn")):
        print(
            "The registry needs FastAPI and uvicorn: "
            'pip install "datalab-capsule[registry]"',
            file=sys.stderr,
        )
        return 1
    from datalab_capsule.registry_app import (  # pylint: disable=import-outside-toplevel
        main as serve,
    )

    return serve(argv)


if __name__ == "__main__":
    sys.exit(main())
