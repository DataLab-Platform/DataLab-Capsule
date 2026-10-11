# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Capsule registry: manifests, declared attachments and reviews, never workspaces."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from datalab_capsule.archive import create_from_hdf5
from datalab_capsule.manifest import MANIFEST_NAME, manifest_fingerprint
from datalab_capsule.review import create_review

from .test_capsule import DATE, workspace_bytes

# The registry is a server component: Pyodide has no sqlite3 module by default.
pytest.importorskip("sqlite3")

from datalab_capsule.registry import (  # noqa: E402
    Registry,
    RegistryError,
    RegistryNotFound,
)

NOTE = {
    "name": "protocol.txt",
    "data": b"Signals acquired on bench 2.\n",
    "media_type": "text/plain",
    "description": "Protocol",
}


def _manifest_bytes(capsule: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(capsule)) as archive:
        return archive.read(MANIFEST_NAME)


@pytest.fixture(name="published")
def _published(tmp_path: Path) -> tuple[Registry, bytes, bytes]:
    """A registry holding one parent revision and one child with an attachment."""
    workspace = workspace_bytes()
    parent = _manifest_bytes(create_from_hdf5(workspace, date_published=DATE))
    child = _manifest_bytes(
        create_from_hdf5(
            workspace,
            name="child",
            date_published=DATE,
            revision_of=manifest_fingerprint(json.loads(parent)),
            attachments=[NOTE],
        )
    )
    registry = Registry(tmp_path / "registry")
    registry.publish_manifest(parent)
    registry.publish_manifest(child)
    return registry, parent, child


def test_publish_list_and_get(published) -> None:
    """Revisions are listed flat with their parent; manifests come back as sent."""
    registry, parent, child = published
    parent_fp = manifest_fingerprint(json.loads(parent))
    child_fp = manifest_fingerprint(json.loads(child))
    listed = registry.list_revisions()
    assert [r["fingerprint"] for r in listed] == [child_fp, parent_fp]
    assert listed[0]["revision_of"] == parent_fp
    assert listed[0]["attachments"][0]["name"] == "protocol.txt"
    assert listed[0]["attachments"][0]["stored"] is False
    assert registry.get_manifest(child_fp) == child
    # Publishing again changes nothing.
    assert registry.publish_manifest(child)["created"] is False
    assert len(registry.list_revisions()) == 2


def test_registry_stores_no_workspace(published) -> None:
    """Only manifests, reviews and attachments are stored, never HDF5."""
    registry = published[0]
    files = [p for p in registry.root.rglob("*") if p.is_file()]
    assert not [p for p in files if p.suffix in (".h5", ".hdf5")]
    assert not [p for p in files if p.read_bytes().startswith(b"\x89HDF")]


def test_invalid_manifests_are_refused(tmp_path: Path) -> None:
    """Tampered, non-JSON or oversized manifests are refused."""
    registry = Registry(tmp_path, max_manifest_bytes=200_000)
    manifest = json.loads(
        _manifest_bytes(create_from_hdf5(workspace_bytes(), date_published=DATE))
    )
    next(e for e in manifest["@graph"] if e["@id"] == "./")["name"] = "edited"
    with pytest.raises(RegistryError, match="fingerprint"):
        registry.publish_manifest(json.dumps(manifest))
    with pytest.raises(RegistryError, match="not valid JSON"):
        registry.publish_manifest(b"{not json")
    with pytest.raises(RegistryError, match="exceeds"):
        registry.publish_manifest(b" " * 200_001)
    assert registry.list_revisions() == []


def test_attachments_are_checked_against_the_manifest(published) -> None:
    """Only declared attachments with the declared size and SHA-256 are stored."""
    registry, parent, child = published
    child_fp = manifest_fingerprint(json.loads(child))
    with pytest.raises(RegistryError, match="does not match"):
        registry.put_attachment(child_fp, "protocol.txt", b"other bytes")
    with pytest.raises(RegistryNotFound):
        registry.put_attachment(child_fp, "other.txt", NOTE["data"])
    with pytest.raises(RegistryError, match="Invalid attachment name"):
        registry.put_attachment(child_fp, "../protocol.txt", NOTE["data"])
    with pytest.raises(RegistryNotFound, match="not uploaded"):
        registry.get_attachment(child_fp, "protocol.txt")
    registry.put_attachment(child_fp, "protocol.txt", NOTE["data"])
    assert registry.get_attachment(child_fp, "protocol.txt") == NOTE["data"]
    assert registry.get_revision(child_fp)["attachments"][0]["stored"] is True
    # A stored file altered on disk is detected when read.
    [blob] = list((registry.root / "attachments").iterdir())
    blob.write_bytes(b"altered")
    with pytest.raises(RegistryError, match="altered"):
        registry.get_attachment(child_fp, "protocol.txt")
    del parent


def test_reviews_target_published_revisions(published) -> None:
    """A review is stored only for a published revision it really targets."""
    registry, parent, child = published
    child_fp = manifest_fingerprint(json.loads(child))
    review = create_review(
        json.loads(child), reviewer="B", statement="Replayed.", verdict="endorsed"
    )
    result = registry.publish_review(json.dumps(review))
    assert result == {
        "fingerprint": review["review_fingerprint"],
        "target": child_fp,
        "created": True,
    }
    assert registry.publish_review(json.dumps(review))["created"] is False
    assert registry.list_reviews(child_fp) == [review]
    assert registry.get_revision(child_fp)["reviews"] == 1
    assert json.loads(registry.get_review(review["review_fingerprint"])) == review
    tampered = {**review, "statement": "Edited."}
    with pytest.raises(RegistryError, match="fingerprint"):
        registry.publish_review(json.dumps(tampered))
    unknown = create_review("sha256:" + "0" * 64, reviewer="B", statement="?")
    with pytest.raises(RegistryNotFound):
        registry.publish_review(json.dumps(unknown))
    with pytest.raises(RegistryError, match="Invalid fingerprint"):
        registry.get_review("../../registry.sqlite3")
    del parent


def test_registry_persists(published) -> None:
    """A registry reopened from its directory holds the same revisions."""
    registry = published[0]
    assert Registry(registry.root).list_revisions() == registry.list_revisions()


# -- HTTP interface -----------------------------------------------------------


@pytest.fixture(name="client")
def _client(published):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from datalab_capsule.registry_app import create_app

    registry, parent, child = published
    app = create_app(registry, token="secret", origins=["https://web.example"])
    return TestClient(app), registry, parent, child


AUTH = {"Authorization": "Bearer secret"}


def test_http_reading_is_open_and_publishing_needs_the_token(client) -> None:
    """Reads need no token; publishing does."""
    http, registry, parent, child = client
    child_fp = manifest_fingerprint(json.loads(child))
    assert http.get("/api/v1/status").json()["token_required"] is True
    listed = http.get("/api/v1/revisions").json()
    assert [r["fingerprint"] for r in listed][0] == child_fp
    assert http.get(f"/api/v1/revisions/{child_fp}").content == child
    url = f"/api/v1/revisions/{child_fp}/attachments/protocol.txt"
    assert http.put(url, content=NOTE["data"]).status_code == 401
    wrong = {"Authorization": "Bearer other"}
    assert http.put(url, content=NOTE["data"], headers=wrong).status_code == 401
    assert http.put(url, content=NOTE["data"], headers=AUTH).status_code == 204
    response = http.get(url)
    assert response.content == NOTE["data"]
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    del registry, parent


def test_http_publish_and_errors(client) -> None:
    """Publishing returns 201 then 200; invalid documents and unknowns are reported."""
    http, registry, parent, child = client
    other = _manifest_bytes(
        create_from_hdf5(workspace_bytes(), name="other", date_published=DATE)
    )
    assert http.post("/api/v1/revisions", content=other).status_code == 401
    created = http.post("/api/v1/revisions", content=other, headers=AUTH)
    assert created.status_code == 201
    again = http.post("/api/v1/revisions", content=other, headers=AUTH)
    assert again.status_code == 200 and again.json()["created"] is False
    refused = http.post("/api/v1/revisions", content=b"{}", headers=AUTH)
    assert refused.status_code == 422
    missing = http.get("/api/v1/revisions/sha256:" + "0" * 64)
    assert missing.status_code == 404
    assert http.get("/api/v1/revisions/not-a-fingerprint").status_code == 422
    big = http.post(
        "/api/v1/revisions",
        content=b" " * (registry.max_manifest_bytes + 1),
        headers=AUTH,
    )
    assert big.status_code == 413
    review = create_review(json.loads(child), reviewer="B", statement="Checked.")
    published = http.post("/api/v1/reviews", content=json.dumps(review), headers=AUTH)
    assert published.status_code == 201
    child_fp = manifest_fingerprint(json.loads(child))
    assert http.get(f"/api/v1/revisions/{child_fp}/reviews").json() == [review]
    fingerprint = review["review_fingerprint"]
    assert http.get(f"/api/v1/reviews/{fingerprint}").json() == review
    del parent


def test_http_cross_origin_and_openapi(client) -> None:
    """The configured origin may call the registry; the API is described."""
    http = client[0]
    preflight = http.options(
        "/api/v1/revisions",
        headers={
            "Origin": "https://web.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://web.example"
    assert "access-control-allow-credentials" not in preflight.headers
    other = http.get("/api/v1/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    paths = http.get("/openapi.json").json()["paths"]
    assert "/api/v1/revisions/{fingerprint}/attachments/{name}" in paths
