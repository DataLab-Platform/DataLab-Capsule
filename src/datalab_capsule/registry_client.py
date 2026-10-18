# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Client of a capsule registry, with the standard library only.

The registry is never trusted: a fetched manifest must be valid and have the
requested fingerprint, each review is validated against it, and each attachment
must match the SHA-256 and size the manifest declares. Publishing needs the
registry's bearer token, given by the caller (the command line reads it from an
environment variable).
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from datalab_capsule.archive import MAX_ATTACHMENT_BYTES, read_capsule
from datalab_capsule.integrity import ProvenanceError, load_json, sha256_digest
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    check_attachment_name,
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.registry import MAX_REGISTRY_MANIFEST_BYTES
from datalab_capsule.review import validate_review

__all__ = [
    "MAX_LISTING_BYTES",
    "MAX_REPLY_BYTES",
    "RegistryClient",
    "RegistryClientError",
    "fetch_revision",
]

#: Seconds before a registry request is abandoned.
TIMEOUT = 30
#: Largest listing read (revisions, or the reviews of a revision).
MAX_LISTING_BYTES = 16 * 1024 * 1024
#: Largest short reply read (publication result, error, status).
MAX_REPLY_BYTES = 64 * 1024


class RegistryClientError(ProvenanceError):
    """Raised when the registry refuses a request or returns invalid data."""


def _read(response: Any, limit: int) -> bytes:
    """Read a response body, refusing it as soon as it exceeds *limit* bytes."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise RegistryClientError(f"The registry response exceeds {limit} bytes")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RegistryClientError(f"The registry response exceeds {limit} bytes")
    return data


class RegistryClient:
    """HTTP client of a registry served by ``workspace-capsule-registry``.

    Args:
        url: Base address (``http://`` or ``https://``).
        token: Bearer token required to publish, or None.
    """

    def __init__(self, url: str, token: str | None = None) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RegistryClientError(f"Not an http(s) address: {url}")
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        kind: str = "",
        limit: int = MAX_REPLY_BYTES,
    ) -> bytes:
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if kind:
            request.add_header("Content-Type", kind)
        if method != "GET" and self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                return _read(response, limit)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(_read(exc, MAX_REPLY_BYTES)).get(
                    "detail", exc.reason
                )
            except (ValueError, AttributeError, RegistryClientError):
                detail = exc.reason
            raise RegistryClientError(f"Registry error {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RegistryClientError(f"Registry unreachable: {exc.reason}") from None

    @staticmethod
    def _revision(fingerprint: str) -> str:
        return "/revisions/" + urllib.parse.quote(fingerprint, safe="")

    def _attachment(self, fingerprint: str, name: str) -> str:
        return (
            self._revision(fingerprint)
            + "/attachments/"
            + urllib.parse.quote(name, safe="")
        )

    def status(self) -> dict[str, Any]:
        """``{"registry", "version", "token_required"}`` of the registry."""
        return load_json(self._request("GET", "/status"))

    def list_revisions(self) -> list[dict[str, Any]]:
        """Flat list of revisions, most recently published first."""
        return load_json(self._request("GET", "/revisions", limit=MAX_LISTING_BYTES))

    def get_manifest(self, fingerprint: str) -> bytes:
        """Manifest as published (not checked)."""
        return self._request(
            "GET", self._revision(fingerprint), limit=MAX_REGISTRY_MANIFEST_BYTES
        )

    def list_reviews(self, fingerprint: str) -> list[Any]:
        """Reviews of a revision (not checked)."""
        return load_json(
            self._request(
                "GET",
                self._revision(fingerprint) + "/reviews",
                limit=MAX_LISTING_BYTES,
            )
        )

    def get_attachment(
        self, fingerprint: str, name: str, max_bytes: int = MAX_ATTACHMENT_BYTES
    ) -> bytes:
        """Attachment bytes (not checked), at most *max_bytes*."""
        return self._request(
            "GET", self._attachment(fingerprint, name), limit=max_bytes
        )

    def get_checked_attachment(
        self, fingerprint: str, name: str, sha256: str, size: int | str
    ) -> bytes:
        """Attachment bytes checked against the SHA-256 and size of its manifest.

        Args:
            fingerprint: Revision fingerprint.
            name: Attachment name.
            sha256: Hexadecimal SHA-256 declared by the checked manifest (not by
             the registry's listing).
            size: Size declared by the checked manifest.

        Raises:
            RegistryClientError: If the registry refuses the request or returns
             other bytes.
        """
        size = int(size)
        data = self.get_attachment(fingerprint, name, min(size, MAX_ATTACHMENT_BYTES))
        digest = sha256_digest(data).split(":", 1)[1]
        if len(data) != size or digest != sha256.split(":")[-1]:
            raise RegistryClientError(
                f"The attachment from the registry does not match the manifest: {name}"
            )
        return data

    def publish_manifest(self, manifest: bytes) -> dict[str, Any]:
        """Publish a manifest (JSON bytes): ``{"fingerprint", "created"}``."""
        return load_json(
            self._request("POST", "/revisions", manifest, "application/json")
        )

    def upload_attachment(self, fingerprint: str, name: str, data: bytes) -> None:
        """Upload an attachment declared by a published manifest."""
        self._request(
            "PUT",
            self._attachment(fingerprint, name),
            data,
            "application/octet-stream",
        )

    def publish_review(self, review: bytes | str) -> dict[str, Any]:
        """Publish a review (JSON): ``{"fingerprint", "target", "created"}``."""
        data = review.encode("utf-8") if isinstance(review, str) else review
        return load_json(self._request("POST", "/reviews", data, "application/json"))

    def publish_capsule(
        self, capsule: str | bytes, reviews: list[bytes] = ()
    ) -> dict[str, Any]:
        """Publish a capsule's manifest and attachments, then *reviews*.

        The workspace is never sent. Returns ``{"fingerprint", "created",
        "attachments", "reviews"}`` (counts of what was sent).
        """
        raw = capsule if isinstance(capsule, bytes) else Path(capsule).read_bytes()
        read = read_capsule(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = archive.read(MANIFEST_NAME)
        result = self.publish_manifest(manifest)
        fingerprint = result["fingerprint"]
        for name, data in read.attachments.items():
            self.upload_attachment(fingerprint, name, data)
        for review in reviews:
            self.publish_review(review)
        return {
            "fingerprint": fingerprint,
            "created": result["created"],
            "attachments": len(read.attachments),
            "reviews": len(reviews),
        }


def fetch_revision(client: RegistryClient, fingerprint: str) -> dict[str, Any]:
    """Fetch a revision and check everything against its manifest.

    Returns:
        ``{"manifest", "reviews", "attachments"}``: the checked manifest; each
        review as ``{"review", "status"}`` (``"valid"`` or the reason); each
        declared attachment as ``{"name", "data", "status"}`` (``data`` is None
        unless ``status`` is ``"valid"``).

    Raises:
        RegistryClientError: If the manifest is invalid or another revision.
    """
    try:
        manifest = load_json(client.get_manifest(fingerprint))
        validate_manifest(manifest)
    except (ValueError, ProvenanceError) as exc:
        raise RegistryClientError(f"Invalid manifest from the registry: {exc}") from exc
    actual = manifest_fingerprint(manifest)
    if actual != fingerprint:
        raise RegistryClientError(
            f"The registry returned another revision: {actual}, not {fingerprint}"
        )
    reviews = []
    for review in client.list_reviews(fingerprint):
        try:
            validate_review(review, manifest)
            reviews.append({"review": review, "status": "valid"})
        except ProvenanceError as exc:
            reviews.append({"review": review, "status": str(exc)})
    attachments = []
    for declared in inspect_manifest(manifest)["attachments"]:
        name, data, status = declared["name"], None, "valid"
        # The name is written as a file name by callers: never trust it unchecked
        check_attachment_name(name)
        try:
            fetched = client.get_attachment(
                fingerprint, name, min(int(declared["size"]), MAX_ATTACHMENT_BYTES)
            )
        except RegistryClientError as exc:
            status = str(exc)
        else:
            digest = sha256_digest(fetched).split(":", 1)[1]
            if digest != declared["sha256"] or str(len(fetched)) != declared["size"]:
                status = "does not match the manifest"
            else:
                data = fetched
        attachments.append({"name": name, "data": data, "status": status})
    return {"manifest": manifest, "reviews": reviews, "attachments": attachments}
