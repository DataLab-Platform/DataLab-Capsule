# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Review assertions on capsule revisions.

A review is a separate JSON document stating what a reviewer concludes about one
capsule revision, identified by its manifest fingerprint. It never changes the
capsule; a capsule may have any number of reviews. The review carries its own
fingerprint (RFC 8785 canonical JSON without the fingerprint property), so that
it can itself be referenced and checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from datalab_capsule._schema import schema_errors
from datalab_capsule.integrity import ProvenanceError, json_digest
from datalab_capsule.ledger import utc_timestamp
from datalab_capsule.manifest import manifest_fingerprint

__all__ = [
    "REVIEW_SCHEMA",
    "VERDICTS",
    "ReviewError",
    "create_review",
    "review_fingerprint",
    "validate_review",
]

REVIEW_SCHEMA = "datalab-capsule-review"
#: Verdicts a reviewer may state (or None).
VERDICTS = ("endorsed", "disputed", "comment")
_FINGERPRINT_KEY = "review_fingerprint"


class ReviewError(ProvenanceError, ValueError):
    """Raised when a review assertion is invalid or does not match its capsule."""


def review_fingerprint(review: Mapping[str, Any]) -> str:
    """Return the fingerprint of a review (its own fingerprint excluded)."""
    data = {k: v for k, v in review.items() if k != _FINGERPRINT_KEY}
    return json_digest(data)


def create_review(
    target: Mapping[str, Any] | str,
    *,
    reviewer: str,
    statement: str,
    verdict: str | None = None,
    reviewer_id: str | None = None,
    reports: Iterable[Mapping[str, Any]] = (),
    date: str | None = None,
) -> dict[str, Any]:
    """Create a review assertion on one capsule revision.

    Args:
        target: Reviewed manifest, or its fingerprint.
        reviewer: Reviewer name.
        statement: What the reviewer concludes.
        verdict: ``endorsed``, ``disputed``, ``comment`` or None.
        reviewer_id: Optional persistent identifier of the reviewer.
        reports: Verification reports the review relies on (``activity_id`` and
         ``verdict`` are kept).
        date: ISO 8601 date (defaults to now).

    Returns:
        The review, with its fingerprint.

    Raises:
        ReviewError: If the review is invalid.
    """
    fingerprint = target if isinstance(target, str) else manifest_fingerprint(target)
    review = {
        "review": REVIEW_SCHEMA,
        "review_version": 1,
        "target": fingerprint,
        "reviewer": {"name": reviewer, "identifier": reviewer_id},
        "date": date or utc_timestamp(),
        "statement": statement,
        "verdict": verdict,
        "reports": [
            {"activity_id": r["activity_id"], "verdict": r["verdict"]} for r in reports
        ],
    }
    review[_FINGERPRINT_KEY] = review_fingerprint(review)
    validate_review(review)
    return review


def validate_review(review: Any, manifest: Mapping[str, Any] | None = None) -> None:
    """Validate a review and, when given, check that it targets *manifest*.

    Args:
        review: Review assertion.
        manifest: The capsule manifest the review should target.

    Raises:
        ReviewError: If the review is invalid, altered, or targets another
         revision.
    """
    errors = schema_errors("review-1", review)
    if errors:
        raise ReviewError("Invalid review: " + "; ".join(errors[:5]))
    if review[_FINGERPRINT_KEY] != review_fingerprint(review):
        raise ReviewError("The review fingerprint does not match its content")
    if manifest is not None and review["target"] != manifest_fingerprint(manifest):
        raise ReviewError("The review targets another capsule revision")
