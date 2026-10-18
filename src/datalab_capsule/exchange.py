# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Capsule exchange functions shared by the editions' capsule interfaces.

The editions (DataLab Desktop, DataLab-Web) offer the same capsule dialog:
export, inspect, open as workspace, write a review, compare revisions and
exchange them through a registry. This module holds what those interfaces need
beyond :mod:`datalab_capsule.archive` and :mod:`datalab_capsule.verify`, so that
both editions give the same answers. It never computes anything: replaying an
activity is the editions' job.

Two results of an inspection are kept apart, and neither is a replay:

- ``integrity``: the archive, the manifest, the workspace (when included or
  supplied) and every stored state;
- ``documents``: verification reports and reviews supplied by the user, checked
  against the capsule. Their verdicts are what their authors claim.

Only the standard library and ``jsonschema`` are needed (``h5py`` for the stored
states of a workspace, as in :func:`datalab_capsule.verify.verify_capsule`).
"""

from __future__ import annotations

import collections
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from datalab_capsule.archive import (
    DEFAULT_SIZE_POLICY,
    CapsuleError,
    SizePolicy,
    read_capsule,
)
from datalab_capsule.diff import diff_manifests
from datalab_capsule.integrity import ProvenanceError, load_json
from datalab_capsule.manifest import (
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.review import create_review, validate_review
from datalab_capsule.verify import verify_capsule

__all__ = [
    "attachment_name",
    "capsule_attachment",
    "capsule_origin",
    "check_registry_revision",
    "diff_checked_manifests",
    "inspect_capsule",
    "open_capsule",
    "review_of",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: Longest base of a proposed attachment name (room left for a ``N-`` prefix).
_NAME_BASE_LENGTH = 96


def _resolver(workspace: bytes | None) -> Callable[[str], bytes] | None:
    """Resolver of a referenced workspace: the file the user selected."""
    return None if workspace is None else (lambda _iri: workspace)


def attachment_name(filename: str, taken: Iterable[str] = ()) -> str:
    """Return an attachment name allowed by the capsule profile.

    Characters other than letters, digits, ``.``, ``_`` and ``-`` are replaced,
    the name starts with a letter or a digit and is made unique among *taken*
    by a ``2-``, ``3-``… prefix.

    Args:
        filename: Name of the file selected by the user (no directory).
        taken: Names already used in the capsule.
    """
    taken = set(taken)
    safe = _UNSAFE.sub("_", filename)
    base = (safe if safe[:1].isalnum() else f"file{safe}")[:_NAME_BASE_LENGTH]
    name, index = base, 2
    while name in taken:
        name, index = f"{index}-{base}", index + 1
    return name


def capsule_origin(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"fingerprint", "name", "attachments"}`` of a capsule manifest.

    It is what an edition remembers of the capsule a workspace was opened from:
    the parent of a revision exported later, and its attachments (which are not
    copied into the revision).
    """
    summary = inspect_manifest(manifest)
    return {
        "fingerprint": manifest_fingerprint(manifest),
        "name": summary["name"],
        "attachments": [a["name"] for a in summary["attachments"]],
    }


def open_capsule(
    data: bytes,
    workspace: bytes | None = None,
    policy: SizePolicy = DEFAULT_SIZE_POLICY,
) -> tuple[bytes, dict[str, Any]]:
    """Validate a capsule and return its workspace bytes and its origin.

    Args:
        data: Capsule bytes.
        workspace: File selected by the user for a referenced workspace; it must
         match the SHA-256 and size of the manifest.
        policy: Size limits of the calling edition.

    Returns:
        ``(workspace, origin)``; *origin* is :func:`capsule_origin`.

    Raises:
        CapsuleError: If the capsule is invalid, or references a workspace that
         was not supplied or does not match.
    """
    capsule = read_capsule(data, policy, _resolver(workspace))
    if capsule.workspace is None:
        raise CapsuleError(
            "This capsule references its workspace: select the workspace file"
        )
    return capsule.workspace, capsule_origin(capsule.manifest)


def capsule_attachment(
    data: bytes, name: str, policy: SizePolicy = DEFAULT_SIZE_POLICY
) -> bytes:
    """Return the checked bytes of one attachment, for an explicit download.

    The capsule is read like any other (archive, manifest, SHA-256 and size of
    every attachment); the attachment is returned as data, never opened.

    Raises:
        CapsuleError: If the capsule is invalid or has no such attachment.
    """
    capsule = read_capsule(data, policy)
    if name not in capsule.attachments:
        raise CapsuleError(f"No such attachment: {name}")
    return capsule.attachments[name]


def inspect_capsule(
    data: bytes,
    *,
    workspace: bytes | None = None,
    reports: Sequence[str | bytes] = (),
    reviews: Sequence[str | bytes] = (),
    policy: SizePolicy = DEFAULT_SIZE_POLICY,
) -> dict[str, Any]:
    """Inspect a capsule without opening it; compute nothing.

    Args:
        data: Capsule bytes.
        workspace: File selected by the user for a referenced workspace.
        reports: Verification reports supplied by the user (JSON texts).
        reviews: Reviews supplied by the user (JSON texts).
        policy: Size limits of the calling edition.

    Returns:
        ``{"summary", "manifest", "integrity", "documents"}``. ``summary`` (see
        :func:`~datalab_capsule.manifest.inspect_manifest`) and ``manifest`` are
        None when the capsule cannot be read. ``integrity`` is ``{"valid",
        "fingerprint", "workspace", "states_located", "states_altered",
        "problems"}``: the problems of the supplied documents are not integrity
        problems. ``documents`` is ``{"reports", "reviews", "unreadable"}``
        (None when the capsule cannot be read): each entry has a ``status``,
        ``"valid"`` or the reason it is not; unreadable documents are
        ``{"kind", "index", "status"}``.
    """
    parsed: dict[str, list[Any]] = {"reports": [], "reviews": []}
    unreadable: list[dict[str, Any]] = []
    for kind, texts in (("reports", reports), ("reviews", reviews)):
        for index, text in enumerate(texts):
            try:
                parsed[kind].append(load_json(text))
            except ValueError as exc:
                unreadable.append(
                    {"kind": kind[:-1], "index": index, "status": str(exc)}
                )
    resolver = _resolver(workspace)
    try:
        # Without the referenced workspace: a workspace that does not match is an
        # integrity problem (see verify_capsule), the capsule stays readable
        capsule = read_capsule(data, policy)
    except (OSError, ProvenanceError) as exc:
        return {
            "summary": None,
            "manifest": None,
            "integrity": {
                "valid": False,
                "fingerprint": None,
                "workspace": None,
                "states_located": None,
                "states_altered": [],
                "problems": [f"Invalid capsule: {exc}"],
            },
            "documents": None,
        }
    manifest = capsule.manifest
    # Free the workspace copy before verify_capsule reads the archive again.
    del capsule
    result = verify_capsule(
        data,
        resolve_workspace=resolver,
        reports=parsed["reports"],
        reviews=parsed["reviews"],
        policy=policy,
    )
    rejected = collections.Counter(
        entry["status"]
        for entry in result["reports"] + result["reviews"]
        if entry["status"] != "valid"
    )
    problems = []
    for problem in result["problems"]:
        if rejected[problem]:
            rejected[problem] -= 1
        else:
            problems.append(problem)
    return {
        "summary": inspect_manifest(manifest),
        "manifest": manifest,
        "integrity": {
            "valid": not problems,
            "fingerprint": result["fingerprint"],
            "workspace": result["workspace"],
            "states_located": result["states_located"],
            "states_altered": result["states_altered"],
            "problems": problems,
        },
        "documents": {
            "reports": result["reports"],
            "reviews": result["reviews"],
            "unreadable": unreadable,
        },
    }


def check_registry_revision(
    manifest: str | bytes, fingerprint: str, reviews: Sequence[Any] = ()
) -> dict[str, Any]:
    """Check a revision retrieved from a registry; trust nothing it says.

    Args:
        manifest: Manifest JSON text returned by the registry.
        fingerprint: Fingerprint that was requested.
        reviews: Reviews returned by the registry, as JSON texts or parsed.

    Returns:
        ``{"summary", "manifest", "reviews"}``; each review is ``{"review",
        "status"}``, ``status`` being ``"valid"`` or the reason it is not.

    Raises:
        ProvenanceError: If the manifest is invalid or is another revision.
    """
    parsed = load_json(manifest)
    validate_manifest(parsed)
    actual = manifest_fingerprint(parsed)
    if actual != fingerprint:
        raise CapsuleError(
            f"The registry returned another revision: {actual}, not {fingerprint}"
        )
    checked = []
    for item in reviews:
        try:
            review = load_json(item) if isinstance(item, (str, bytes)) else item
            validate_review(review, parsed)
            status = "valid"
        except (ValueError, ProvenanceError) as exc:
            review, status = None, str(exc)
        checked.append({"review": review, "status": status})
    return {
        "summary": inspect_manifest(parsed),
        "manifest": parsed,
        "reviews": checked,
    }


def diff_checked_manifests(old: Any, new: Any) -> dict[str, Any]:
    """Validate two capsule manifests, then compare them (see ``diff_manifests``).

    Raises:
        ManifestError: If a manifest is invalid.
    """
    for manifest in (old, new):
        validate_manifest(manifest)
    return diff_manifests(old, new)


def review_of(
    manifest: Any,
    *,
    reviewer: str,
    statement: str,
    verdict: str | None = None,
    reports: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Write a review of one capsule revision (a separate JSON document).

    Raises:
        ManifestError: If the manifest is invalid.
        ReviewError: If a field of the review is invalid.
    """
    validate_manifest(manifest)
    return create_review(
        manifest,
        reviewer=reviewer,
        statement=statement,
        verdict=verdict,
        reports=reports,
    )
