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

from datalab_capsule.archive import read_capsule
from datalab_capsule.integrity import ProvenanceError, load_json, sha256_digest
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    inspect_manifest,
    manifest_fingerprint,
    validate_manifest,
)
from datalab_capsule.review import validate_review

__all__ = ["RegistryClient", "RegistryClientError", "fetch_revision"]

#: Seconds before a registry request is abandoned.
TIMEOUT = 30


class RegistryClientError(ProvenanceError):
    """Raised when the registry refuses a request or returns invalid data."""


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
        self, method: str, path: str, data: bytes | None = None, kind: str = ""
    ) -> bytes:
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if kind:
            request.add_header("Content-Type", kind)
        if method != "GET" and self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail", exc.reason)
            except ValueError:
                detail = exc.reason
            raise RegistryClientError(f"Registry error {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RegistryClientError(f"Registry unreachable: {exc.reason}") from None

    @staticmethod
    def _revision(fingerprint: str) -> str:
        return "/revisions/" + urllib.parse.quote(fingerprint, safe="")

    def list_revisions(self) -> list[dict[str, Any]]:
        """Flat list of revisions, most recently published first."""
        return load_json(self._request("GET", "/revisions"))

    def get_manifest(self, fingerprint: str) -> bytes:
        """Manifest as published (not checked)."""
        return self._request("GET", self._revision(fingerprint))

    def list_reviews(self, fingerprint: str) -> list[Any]:
        """Reviews of a revision (not checked)."""
        return load_json(self._request("GET", self._revision(fingerprint) + "/reviews"))

    def get_attachment(self, fingerprint: str, name: str) -> bytes:
        """Attachment bytes (not checked)."""
        path = self._revision(fingerprint) + "/attachments/"
        return self._request("GET", path + urllib.parse.quote(name, safe=""))

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
        result = load_json(
            self._request("POST", "/revisions", manifest, "application/json")
        )
        fingerprint = result["fingerprint"]
        for name, data in read.attachments.items():
            path = self._revision(fingerprint) + "/attachments/"
            self._request(
                "PUT",
                path + urllib.parse.quote(name, safe=""),
                data,
                "application/octet-stream",
            )
        for review in reviews:
            self._request("POST", "/reviews", review, "application/json")
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
        try:
            fetched = client.get_attachment(fingerprint, name)
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
