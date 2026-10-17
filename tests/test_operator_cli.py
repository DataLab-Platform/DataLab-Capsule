# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Operator commands: create and review capsules, exchange them through a registry."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from datalab_capsule.archive import read_capsule
from datalab_capsule.cli import main
from datalab_capsule.manifest import manifest_fingerprint
from datalab_capsule.review import validate_review

from .test_capsule import WORKSPACES


def _capsule(tmp_path: Path, name: str, *extra: str) -> Path:
    output = tmp_path / f"{name}.dlcapsule"
    workspace = str(WORKSPACES / "desktop_chain.h5")
    assert main(["create", workspace, str(output), "--name", name, *extra]) == 0
    return output


def test_create_and_review(tmp_path: Path, capsys) -> None:
    """A Desktop operator creates a capsule, a child revision and a review."""
    note = tmp_path / "protocol.txt"
    note.write_bytes(b"Bench 2.\n")
    parent = _capsule(tmp_path, "parent", "--attach", str(note))
    assert "1 attachments" in capsys.readouterr().out
    attached = read_capsule(parent)
    assert attached.attachments == {"protocol.txt": b"Bench 2.\n"}
    child = _capsule(tmp_path, "child", "--revision-of", str(parent))
    parent_fp = manifest_fingerprint(attached.manifest)
    root = next(e for e in read_capsule(child).manifest["@graph"] if e["@id"] == "./")
    assert root["dlc:revisionOf"] == parent_fp
    # Never overwrite an existing file.
    assert main(["create", str(WORKSPACES / "desktop_chain.h5"), str(child)]) == 1
    review = tmp_path / "review.json"
    args = ["review", str(child), str(review), "--reviewer", "A"]
    assert main([*args, "--statement", "Replayed.", "--verdict", "endorsed"]) == 0
    validate_review(json.loads(review.read_text("utf-8")), read_capsule(child).manifest)
    assert main(["verify", str(child), "--review", str(review)]) == 0
    assert main(["diff", str(parent), str(child)]) == 1
    assert "Attachment changes (1)" in capsys.readouterr().out
    extracted = tmp_path / "child.h5"
    assert main(["workspace", str(child), str(extracted)]) == 0
    assert extracted.read_bytes() == read_capsule(child).workspace
    assert main(["workspace", str(child), str(extracted)]) == 1


def test_create_without_h5py_says_what_to_install(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Without the hdf5 extra, ``create`` explains instead of failing."""
    monkeypatch.setitem(sys.modules, "h5py", None)
    workspace = str(WORKSPACES / "desktop_chain.h5")
    assert main(["create", workspace, str(tmp_path / "x.dlcapsule")]) == 1
    assert "DataLab-Capsule[hdf5]" in capsys.readouterr().err
    assert not (tmp_path / "x.dlcapsule").exists()


@pytest.fixture(name="server")
def _server(tmp_path: Path):
    """A real registry served by uvicorn in a thread."""
    pytest.importorskip("sqlite3")
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    from datalab_capsule.registry import Registry
    from datalab_capsule.registry_app import create_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(Registry(tmp_path / "registry"), token="secret")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_registry_round_trip(tmp_path: Path, server: str, capsys, monkeypatch) -> None:
    """Publish with the token, list, then fetch and check into a new folder."""
    note = tmp_path / "protocol.txt"
    note.write_bytes(b"Bench 2.\n")
    capsule = _capsule(tmp_path, "chain", "--attach", str(note))
    review = tmp_path / "review.json"
    assert main(["review", str(capsule), str(review), "--reviewer", "A",
                 "--statement", "Checked."]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["registry", "publish", server, str(capsule)]) == 1
    assert "401" in capsys.readouterr().err
    monkeypatch.setenv("REGISTRY_TOKEN", "secret")
    publish = ["registry", "publish", server, str(capsule), "--review", str(review)]
    assert main([*publish, "--token-env", "REGISTRY_TOKEN"]) == 0
    assert "1 attachments and 1 reviews sent" in capsys.readouterr().out
    assert main(["registry", "list", server, "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    fingerprint = manifest_fingerprint(read_capsule(capsule).manifest)
    assert listed["fingerprint"] == fingerprint and listed["reviews"] == 1
    folder = tmp_path / "fetched"
    assert main(["registry", "fetch", server, fingerprint, str(folder)]) == 0
    out = capsys.readouterr().out
    assert "Review 1: valid, A" in out
    assert (folder / "attachments" / "protocol.txt").read_bytes() == b"Bench 2.\n"
    # A fetched manifest compares like a capsule.
    assert main(["diff", str(capsule), str(folder / "manifest.json")]) == 0
    # The folder is never reused.
    assert main(["registry", "fetch", server, fingerprint, str(folder)]) == 1
    missing = "sha256:" + "0" * 64
    assert main(["registry", "fetch", server, missing, str(tmp_path / "x")]) == 1
    assert "404" in capsys.readouterr().err


def test_registry_client_steps(tmp_path: Path, server: str) -> None:
    """The steps of a capsule interface: status, manifest, attachment, review."""
    from datalab_capsule.exchange import check_registry_revision, review_of
    from datalab_capsule.registry_client import RegistryClient, RegistryClientError

    note = tmp_path / "protocol.txt"
    note.write_bytes(b"Bench 2.\n")
    capsule = read_capsule(_capsule(tmp_path, "steps", "--attach", str(note)))
    manifest = json.dumps(capsule.manifest).encode("utf-8")
    reader = RegistryClient(server)
    assert reader.status()["token_required"] is True
    with pytest.raises(RegistryClientError, match="401"):
        reader.publish_manifest(manifest)
    client = RegistryClient(server, "secret")
    published = client.publish_manifest(manifest)
    fingerprint = published["fingerprint"]
    assert published["created"] is True
    assert fingerprint == manifest_fingerprint(capsule.manifest)
    client.upload_attachment(fingerprint, "protocol.txt", b"Bench 2.\n")
    review = review_of(capsule.manifest, reviewer="B", statement="Checked.")
    assert client.publish_review(json.dumps(review))["target"] == fingerprint
    [listed] = reader.list_revisions()
    assert listed["fingerprint"] == fingerprint and listed["reviews"] == 1
    checked = check_registry_revision(
        reader.get_manifest(fingerprint), fingerprint, reader.list_reviews(fingerprint)
    )
    assert [r["status"] for r in checked["reviews"]] == ["valid"]
    [declared] = checked["summary"]["attachments"]
    data = reader.get_checked_attachment(
        fingerprint, declared["name"], declared["sha256"], declared["size"]
    )
    assert data == b"Bench 2.\n"
    with pytest.raises(RegistryClientError, match="does not match"):
        reader.get_checked_attachment(
            fingerprint, declared["name"], "0" * 64, declared["size"]
        )
