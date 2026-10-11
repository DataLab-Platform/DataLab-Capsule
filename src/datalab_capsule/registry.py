# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Capsule registry: published manifests, their attachments and their reviews.

The registry stores what is needed to find, compare and discuss capsule
revisions, never workspaces: a manifest (validated, identified by its
fingerprint), the attachments it declares (checked against their declared size
and SHA-256), and reviews (checked against the revision they target). Metadata
lives in SQLite, documents and attachments in files. Nothing in a stored file is
executed or interpreted beyond these checks.

This module only needs the standard library; the HTTP interface is in
:mod:`datalab_capsule.registry_app` (``registry`` extra).
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from datalab_capsule.archive import MAX_ATTACHMENT_BYTES
from datalab_capsule.integrity import ProvenanceError, load_json, sha256_digest
from datalab_capsule.ledger import utc_timestamp
from datalab_capsule.manifest import (
    check_attachment_name,
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.review import review_fingerprint, validate_review

__all__ = [
    "MAX_LISTED",
    "MAX_REGISTRY_MANIFEST_BYTES",
    "MAX_REVIEW_BYTES",
    "Registry",
    "RegistryError",
    "RegistryNotFound",
    "check_fingerprint",
]

#: Largest manifest accepted by default (a manifest holds no data arrays).
MAX_REGISTRY_MANIFEST_BYTES = 8 * 1024 * 1024
#: Largest review accepted.
MAX_REVIEW_BYTES = 1024 * 1024
#: Most revisions returned by one listing.
MAX_LISTED = 1000

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    fingerprint TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    revision_of TEXT,
    date_published TEXT NOT NULL,
    workspace_referenced INTEGER NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    fingerprint TEXT NOT NULL REFERENCES revisions(fingerprint),
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    description TEXT,
    stored INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fingerprint, name)
);
CREATE TABLE IF NOT EXISTS reviews (
    fingerprint TEXT PRIMARY KEY,
    target TEXT NOT NULL REFERENCES revisions(fingerprint),
    reviewer TEXT NOT NULL,
    verdict TEXT,
    date TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_target ON reviews(target);
"""


class RegistryError(ProvenanceError, ValueError):
    """Raised when a document is refused by the registry."""


class RegistryNotFound(RegistryError, LookupError):
    """Raised when a revision, attachment or review is not in the registry."""


def check_fingerprint(fingerprint: Any) -> str:
    """Return *fingerprint* if it is a ``sha256:`` digest, else raise.

    Raises:
        RegistryError: If it is not ``sha256:`` followed by 64 hex digits.
    """
    if not isinstance(fingerprint, str) or not _FINGERPRINT.match(fingerprint):
        raise RegistryError(f"Invalid fingerprint: {fingerprint!r}")
    return fingerprint


def _load(data: bytes | str, limit: int, what: str) -> Any:
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > limit:
        raise RegistryError(f"The {what} exceeds {limit} bytes")
    try:
        return load_json(raw)
    except ValueError as exc:
        raise RegistryError(f"The {what} is not valid JSON: {exc}") from exc


class Registry:
    """Registry stored in a directory (``registry.sqlite3`` plus files).

    Args:
        root: Directory of the registry (created if needed).
        max_manifest_bytes: Largest manifest accepted.
        max_attachment_bytes: Largest attachment accepted.
    """

    def __init__(
        self,
        root: str | os.PathLike,
        *,
        max_manifest_bytes: int = MAX_REGISTRY_MANIFEST_BYTES,
        max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.root = Path(root)
        self.max_manifest_bytes = max_manifest_bytes
        self.max_attachment_bytes = max_attachment_bytes
        for folder in ("manifests", "reviews", "attachments"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation, serialised: simple and thread-safe."""
        with self._lock:
            db = sqlite3.connect(self.root / "registry.sqlite3")
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        """Write *data* to *path* atomically (never a partial file)."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _manifest_path(self, fingerprint: str) -> Path:
        return self.root / "manifests" / f"{fingerprint[7:]}.json"

    def _review_path(self, fingerprint: str) -> Path:
        return self.root / "reviews" / f"{fingerprint[7:]}.json"

    def _blob_path(self, sha256: str) -> Path:
        return self.root / "attachments" / sha256

    # -- Revisions ----------------------------------------------------------

    def publish_manifest(self, data: bytes | str) -> dict[str, Any]:
        """Validate and store a manifest; publishing it again changes nothing.

        Args:
            data: Manifest JSON (``ro-crate-metadata.json`` of a capsule).

        Returns:
            The listed revision (see :meth:`list_revisions`) and ``created``.

        Raises:
            RegistryError: If the manifest is too large, not JSON or invalid.
        """
        manifest = _load(data, self.max_manifest_bytes, "manifest")
        try:
            validate_manifest(manifest)
        except ProvenanceError as exc:
            raise RegistryError(str(exc)) from exc
        fingerprint = manifest_fingerprint(manifest)
        summary = inspect_manifest(manifest)
        with self._db() as db:
            exists = db.execute(
                "SELECT 1 FROM revisions WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if not exists:
                self._write(
                    self._manifest_path(fingerprint),
                    data.encode("utf-8") if isinstance(data, str) else bytes(data),
                )
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        fingerprint,
                        summary["name"],
                        summary["description"],
                        summary["revision_of"],
                        summary["date_published"],
                        int(summary["workspace"]["referenced"]),
                        utc_timestamp(),
                    ),
                )
                db.executemany(
                    "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, 0)",
                    [
                        (
                            fingerprint,
                            a["name"],
                            a["media_type"],
                            int(a["size"]),
                            a["sha256"],
                            a["description"],
                        )
                        for a in summary["attachments"]
                    ],
                )
        return {**self.get_revision(fingerprint), "created": not exists}

    def _row(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        fingerprint = row["fingerprint"]
        attachments = db.execute(
            "SELECT name, media_type, size, sha256, description, stored "
            "FROM attachments WHERE fingerprint = ? ORDER BY rowid",
            (fingerprint,),
        ).fetchall()
        reviews = db.execute(
            "SELECT COUNT(*) FROM reviews WHERE target = ?", (fingerprint,)
        ).fetchone()[0]
        return {
            "fingerprint": fingerprint,
            "name": row["name"],
            "description": row["description"],
            "revision_of": row["revision_of"],
            "date_published": row["date_published"],
            "workspace_referenced": bool(row["workspace_referenced"]),
            "published_at": row["published_at"],
            "attachments": [
                {
                    "name": a["name"],
                    "media_type": a["media_type"],
                    "size": a["size"],
                    "sha256": a["sha256"],
                    "description": a["description"],
                    "stored": bool(a["stored"]),
                }
                for a in attachments
            ],
            "reviews": reviews,
        }

    def list_revisions(self, limit: int = MAX_LISTED) -> list[dict[str, Any]]:
        """Return the revisions, most recently published first (flat list).

        Each revision gives its fingerprint, name, description, parent
        (``revision_of``), publication dates, whether its workspace is
        referenced, its declared attachments (``stored`` once uploaded) and its
        number of reviews.
        """
        limit = max(1, min(int(limit), MAX_LISTED))
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM revisions ORDER BY published_at DESC, rowid DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row(db, row) for row in rows]

    def get_revision(self, fingerprint: str) -> dict[str, Any]:
        """Return one listed revision.

        Raises:
            RegistryNotFound: If the revision is not in the registry.
        """
        check_fingerprint(fingerprint)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM revisions WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None:
                raise RegistryNotFound(f"Unknown revision: {fingerprint}")
            return self._row(db, row)

    def get_manifest(self, fingerprint: str) -> bytes:
        """Return the manifest of a revision, as published.

        Raises:
            RegistryNotFound: If the revision is not in the registry.
        """
        self.get_revision(fingerprint)
        return self._manifest_path(fingerprint).read_bytes()

    # -- Attachments --------------------------------------------------------

    def _declared(self, fingerprint: str, name: str) -> sqlite3.Row:
        check_fingerprint(fingerprint)
        try:
            check_attachment_name(name)
        except ProvenanceError as exc:
            raise RegistryError(str(exc)) from exc
        with self._db() as db:
            row = db.execute(
                "SELECT size, sha256, stored FROM attachments "
                "WHERE fingerprint = ? AND name = ?",
                (fingerprint, name),
            ).fetchone()
        if row is None:
            raise RegistryNotFound(f"No attachment {name} in revision {fingerprint}")
        return row

    def put_attachment(self, fingerprint: str, name: str, data: bytes) -> None:
        """Store an attachment declared by a published manifest.

        The bytes must have the declared size and SHA-256. Storing it again
        changes nothing. Identical attachments are stored once.

        Raises:
            RegistryNotFound: If the revision does not declare *name*.
            RegistryError: If the bytes do not match the declaration.
        """
        declared = self._declared(fingerprint, name)
        if len(data) > self.max_attachment_bytes:
            raise RegistryError(
                f"The attachment exceeds {self.max_attachment_bytes} bytes"
            )
        digest = sha256_digest(bytes(data)).split(":", 1)[1]
        if len(data) != declared["size"] or digest != declared["sha256"]:
            raise RegistryError(f"The attachment does not match the manifest: {name}")
        path = self._blob_path(digest)
        if not path.exists():
            self._write(path, bytes(data))
        with self._db() as db:
            db.execute(
                "UPDATE attachments SET stored = 1 WHERE fingerprint = ? AND name = ?",
                (fingerprint, name),
            )

    def get_attachment(self, fingerprint: str, name: str) -> bytes:
        """Return a stored attachment, checked again against its declaration.

        Raises:
            RegistryNotFound: If it is not declared or not uploaded yet.
            RegistryError: If the stored file no longer matches.
        """
        declared = self._declared(fingerprint, name)
        if not declared["stored"]:
            raise RegistryNotFound(f"Attachment not uploaded: {name}")
        data = self._blob_path(declared["sha256"]).read_bytes()
        if sha256_digest(data).split(":", 1)[1] != declared["sha256"]:
            raise RegistryError(f"The stored attachment was altered: {name}")
        return data

    # -- Reviews ------------------------------------------------------------

    def publish_review(self, data: bytes | str) -> dict[str, Any]:
        """Validate and store a review of a published revision.

        Returns:
            ``{"fingerprint", "target", "created"}``.

        Raises:
            RegistryNotFound: If the reviewed revision is not in the registry.
            RegistryError: If the review is invalid or does not target it.
        """
        review = _load(data, MAX_REVIEW_BYTES, "review")
        target = review.get("target") if isinstance(review, dict) else None
        try:
            check_fingerprint(target)
        except RegistryError:
            raise RegistryError("The review has no valid target") from None
        manifest = load_json(self.get_manifest(target))
        try:
            validate_review(review, manifest)
        except ProvenanceError as exc:
            raise RegistryError(str(exc)) from exc
        fingerprint = review_fingerprint(review)
        with self._db() as db:
            exists = db.execute(
                "SELECT 1 FROM reviews WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if not exists:
                self._write(
                    self._review_path(fingerprint),
                    data.encode("utf-8") if isinstance(data, str) else bytes(data),
                )
                db.execute(
                    "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        fingerprint,
                        target,
                        review["reviewer"]["name"],
                        review["verdict"],
                        review["date"],
                        utc_timestamp(),
                    ),
                )
        return {"fingerprint": fingerprint, "target": target, "created": not exists}

    def list_reviews(self, target: str) -> list[dict[str, Any]]:
        """Return the reviews of one revision, oldest first (full documents).

        Raises:
            RegistryNotFound: If the revision is not in the registry.
        """
        self.get_revision(target)
        with self._db() as db:
            rows = db.execute(
                "SELECT fingerprint FROM reviews WHERE target = ? "
                "ORDER BY published_at, rowid",
                (target,),
            ).fetchall()
        return [
            load_json(self._review_path(r["fingerprint"]).read_bytes()) for r in rows
        ]

    def get_review(self, fingerprint: str) -> bytes:
        """Return one review, as published.

        Raises:
            RegistryNotFound: If the review is not in the registry.
        """
        check_fingerprint(fingerprint)
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM reviews WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        if row is None:
            raise RegistryNotFound(f"Unknown review: {fingerprint}")
        return self._review_path(fingerprint).read_bytes()
