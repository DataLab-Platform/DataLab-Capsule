# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Build the public fixture corpus (``tests/fixtures/corpus``).

Sources (``corpus/sources``) are workspaces and verification reports written by
the editions (see ``corpus/README.md``), plus two workspaces derived from them
once with h5py (``python -m tests.corpus --derive``; HDF5 writes are not
byte-reproducible, so their result is committed). Capsules, reviews and invalid
documents are then built deterministically from the sources
(``python -m tests.corpus``); ``test_corpus.py`` checks that the committed files
match a fresh build.

The expected results of the command line are written by hand in
``corpus/expected.json``, never generated.
"""

from __future__ import annotations

import io
import json
import shutil
import stat
import sys
import warnings
import zipfile
from pathlib import Path

import h5py

from datalab_capsule.archive import create_from_hdf5, read_capsule
from datalab_capsule.compare import build_report, compare_environments, compare_exact
from datalab_capsule.hdf5 import locate_current_states, read_block, write_block
from datalab_capsule.integrity import sha256_digest
from datalab_capsule.ledger import Ledger
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    REVISION_KEY,
    WORKSPACE_NAME,
    build_manifest,
    manifest_fingerprint,
)
from datalab_capsule.review import create_review

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
SOURCES = CORPUS / "sources"
CHAINS = Path(__file__).parent / "fixtures" / "workspaces"
DATE = "2026-10-01T00:00:00Z"
LICENSE = "https://spdx.org/licenses/BSD-3-Clause"
REFERENCED_IRI = "https://example.org/datalab/corpus/referenced-workspace.h5"


def derive_sources() -> None:
    """Write the derived workspaces (run once; HDF5 bytes are not reproducible)."""
    for name, edit in (("chain_missing.h5", "delete"), ("chain_altered.h5", "edit")):
        path = SOURCES / name
        shutil.copy(CHAINS / "desktop_chain.h5", path)
        with h5py.File(path, "r+") as h5file:
            ledger, locators = read_block(h5file)
            # The output of the second activity: an intermediate result.
            state_id = ledger.activities[1]["outputs"][0]["state_id"]
            node = locators[state_id]["path"]
            if edit == "delete":
                # Deleted, then saved: its state stays in the ledger, unavailable.
                del h5file[node]
                write_block(h5file, ledger, locate_current_states(h5file, ledger))
            else:
                # Changed in the file after saving: its state is now altered.
                h5file[node]["xydata"][1, 0] += 1.0


def _capsule(source: Path, **kwargs) -> bytes:
    kwargs.setdefault("date_published", DATE)
    kwargs.setdefault("license_", LICENSE)
    return create_from_hdf5(source.read_bytes(), **kwargs)


def _zip(entries: list[tuple[str, bytes]], compress: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive, warnings.catch_warnings():
        # The duplicate-entry fixture is deliberate.
        warnings.simplefilter("ignore", UserWarning)
        for entry, data in entries:
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = (
                zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
            )
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _entries(capsule: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(capsule)) as archive:
        return [(name, archive.read(name)) for name in archive.namelist()]


def _dump(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")


def _json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _refingerprint(manifest: dict) -> dict:
    """Recompute the fingerprint after an edit (to isolate the defect tested)."""
    root = next(e for e in manifest["@graph"] if e["@id"] == "./")
    root.pop("dlc:manifestFingerprint")
    root["dlc:manifestFingerprint"] = manifest_fingerprint(manifest)
    return manifest


def _report(name: str) -> dict:
    return json.loads((SOURCES / f"report_{name}.json").read_text(encoding="utf-8"))


def build() -> dict[str, bytes]:
    """Return ``relative path -> bytes`` of every built corpus file."""
    out: dict[str, bytes] = {}
    chain = CHAINS / "desktop_chain.h5"
    for edition in ("desktop", "web"):
        out[f"valid/chain-{edition}.dlcapsule"] = _capsule(
            CHAINS / f"{edition}_chain.h5",
            name=f"Signal chain ({edition})",
            description="Signal transformations, replayable and opaque.",
        )
        out[f"valid/capture-{edition}.dlcapsule"] = _capsule(
            SOURCES / f"capture_{edition}.h5",
            name=f"Images, ROI and analysis results ({edition})",
            description="Image processing with ROI and analysis results.",
        )
        out[f"valid/difference-{edition}.dlcapsule"] = _capsule(
            SOURCES / f"difference_{edition}.h5",
            name=f"Two-input differences ({edition})",
            description="A - B and B - A on different X grids (recorded alignment).",
        )
    parent = read_capsule(out["valid/chain-desktop.dlcapsule"]).manifest
    out["valid/chain-renamed.dlcapsule"] = _capsule(
        chain,
        name="Signal chain, renamed",
        description="Same workspace; a presentation-only revision.",
        revision_of=manifest_fingerprint(parent),
    )
    out["valid/legacy.dlcapsule"] = _capsule(
        SOURCES / "legacy_imported.h5",
        name="Legacy workspace (DataLab 1.3.0)",
        description="Processing records imported as legacy activities.",
    )
    legacy = read_capsule(out["valid/legacy.dlcapsule"]).manifest
    out["valid/legacy-revision.dlcapsule"] = _capsule(
        SOURCES / "legacy_continued.h5",
        name="Legacy workspace, continued",
        description="The legacy workspace after one more processing.",
        revision_of=manifest_fingerprint(legacy),
    )
    out["valid/missing-resource.dlcapsule"] = _capsule(
        SOURCES / "chain_missing.h5",
        name="Signal chain with a deleted result",
        description="An intermediate result was deleted before saving.",
    )
    out["valid/referenced.dlcapsule"] = _capsule(
        SOURCES / "difference_desktop.h5",
        name="Referenced workspace",
        description="The workspace is referenced, not included.",
        workspace_ref=REFERENCED_IRI,
    )
    difference = read_capsule(out["valid/difference-desktop.dlcapsule"]).manifest
    exact = _report("difference_exact")
    for name in ("difference_exact", "difference_altered", "legacy_opaque"):
        out[f"valid/report-{name.replace('_', '-')}.json"] = _json(_report(name))
    candidate = compare_exact({"y": [1.0, 2.0]}, {"y": [0.0, 1.0]})
    out["valid/report-numeric-different.json"] = _json(
        build_report(
            activity_id=exact["activity_id"],
            restoration="replayable",
            eligibility="ready",
            inputs=exact["inputs"],
            reference=exact["reference"],
            environment=compare_environments(None, None, None, None),
            comparison=candidate,
            reason="Synthetic fixture: reference [0, 1] and candidate [1, 2] "
            "were written by hand, not computed.",
        )
    )
    out["valid/review-endorsed.json"] = _json(
        create_review(
            difference,
            reviewer="Corpus reviewer",
            statement="A - B replays exactly on the recorded environment.",
            verdict="endorsed",
            reports=[exact],
            date=DATE,
        )
    )
    out["valid/review-comment.json"] = _json(
        create_review(
            legacy,
            reviewer="Corpus reviewer",
            statement="Legacy records are kept as stored; nothing is attested.",
            verdict="comment",
            reports=[_report("legacy_opaque")],
            date=DATE,
        )
    )

    # Invalid archives (validate fails).
    small = out["valid/chain-web.dlcapsule"]
    entries = _entries(small)
    manifest = json.loads(entries[0][1])
    tampered = json.loads(entries[0][1])
    next(e for e in tampered["@graph"] if e["@id"] == "./")["name"] = "Tampered"
    out["invalid/tampered-manifest.dlcapsule"] = _zip(
        [(MANIFEST_NAME, _dump(tampered)), entries[1]]
    )
    workspace = bytearray(entries[1][1])
    workspace[-1] ^= 0xFF
    out["invalid/workspace-mismatch.dlcapsule"] = _zip(
        [entries[0], (WORKSPACE_NAME, bytes(workspace))]
    )
    out["invalid/path-traversal.dlcapsule"] = _zip([*entries, ("../evil.txt", b"x")])
    out["invalid/duplicate-entry.dlcapsule"] = _zip([*entries, entries[0]])
    out["invalid/missing-manifest.dlcapsule"] = _zip([entries[1]])
    out["invalid/compressed-entry.dlcapsule"] = _zip(entries, compress=True)
    referenced = _entries(out["valid/referenced.dlcapsule"])
    out["invalid/referenced-with-workspace.dlcapsule"] = _zip(
        [
            referenced[0],
            (WORKSPACE_NAME, (SOURCES / "difference_desktop.h5").read_bytes()),
        ]
    )
    bad_revision = json.loads(entries[0][1])
    next(e for e in bad_revision["@graph"] if e["@id"] == "./")[REVISION_KEY] = (
        "not-a-fingerprint"
    )
    out["invalid/bad-revision.dlcapsule"] = _zip(
        [(MANIFEST_NAME, _dump(_refingerprint(bad_revision))), entries[1]]
    )

    # Valid archives that do not verify.
    with h5py.File(io.BytesIO(entries[1][1]), "r") as h5file:
        ledger, locators = read_block(h5file)
    record = ledger.to_dict()
    record["activities"][0]["call"]["parameters"]["value"] = 2.0
    root = next(e for e in manifest["@graph"] if e["@id"] == "./")
    mismatch = build_manifest(
        Ledger.from_dict(record),
        locators,
        workspace_sha256=sha256_digest(entries[1][1]),
        workspace_size=len(entries[1][1]),
        name="Manifest not matching its ledger",
        description=root["description"],
        license_=root["license"],
        date_published=DATE,
    )
    out["invalid/ledger-mismatch.dlcapsule"] = _zip(
        [(MANIFEST_NAME, _dump(mismatch)), entries[1]]
    )
    out["invalid/altered-state.dlcapsule"] = _capsule(
        SOURCES / "chain_altered.h5",
        name="Signal chain with a result changed after saving",
        description="An intermediate result was changed in the file.",
    )

    # Invalid documents (verify fails when they are supplied).
    review = json.loads(out["valid/review-endorsed.json"])
    wrong = create_review(
        legacy, reviewer="Corpus reviewer", statement="Wrong target.", date=DATE
    )
    out["invalid/review-wrong-target.json"] = _json(wrong)
    review["statement"] = "Edited after fingerprinting."
    out["invalid/review-tampered.json"] = _json(review)
    stranger = dict(exact, activity_id="00000000-0000-4000-8000-0000000000ff")
    out["invalid/report-unknown-activity.json"] = _json(stranger)
    out["invalid/report-invalid.json"] = _json(dict(exact, verdict="probably"))
    return out


def write() -> None:
    """Write the built corpus files."""
    for path, data in build().items():
        (CORPUS / path).write_bytes(data)


if __name__ == "__main__":
    if "--derive" in sys.argv[1:]:
        derive_sources()
    write()
