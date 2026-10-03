# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Workspace capsules: ``.dlcapsule`` ZIP archives holding a workspace and its manifest.

A capsule holds exactly two entries, stored without recompression:
``ro-crate-metadata.json`` (see :mod:`datalab_capsule.manifest`) and
``workspace.h5`` (the DataLab HDF5 workspace, provenance block included).

The reader never executes code, never accesses the network and refuses unsafe
archives before extracting anything: absolute paths, ``..``, drive letters,
backslashes, duplicate or unexpected entries, links, encrypted entries, and
entries over the named limits below.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import zipfile
from typing import Any

from datalab_capsule.hdf5 import ProvenanceFormatError, read_block
from datalab_capsule.integrity import ProvenanceError, sha256_digest
from datalab_capsule.manifest import (
    MANIFEST_NAME,
    WORKSPACE_NAME,
    ManifestError,
    build_manifest,
    validate_manifest,
)

__all__ = [
    "CAPSULE_SUFFIX",
    "MAX_COMPRESSION_RATIO",
    "MAX_ENTRIES",
    "MAX_MANIFEST_BYTES",
    "MAX_WORKSPACE_BYTES",
    "Capsule",
    "CapsuleError",
    "create_from_hdf5",
    "read_capsule",
]

CAPSULE_SUFFIX = ".dlcapsule"
#: Largest number of entries accepted in an archive.
MAX_ENTRIES = 16
#: Largest manifest accepted (bytes, uncompressed).
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
#: Largest workspace accepted (bytes, uncompressed).
MAX_WORKSPACE_BYTES = 4 * 1024**3
#: Largest uncompressed/compressed size ratio accepted for one entry.
MAX_COMPRESSION_RATIO = 100
_LIMITS = {MANIFEST_NAME: MAX_MANIFEST_BYTES, WORKSPACE_NAME: MAX_WORKSPACE_BYTES}


class CapsuleError(ProvenanceError, ValueError):
    """Raised when a capsule cannot be created or is not safe or valid."""


@dataclasses.dataclass(frozen=True)
class Capsule:
    """A validated capsule.

    Attributes:
        manifest: RO-Crate manifest.
        workspace: Bytes of ``workspace.h5``, to pass to the editions' open path.
    """

    manifest: dict[str, Any]
    workspace: bytes


def _read_source(source: str | os.PathLike | bytes) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    with open(source, "rb") as file:
        return file.read()


def create_from_hdf5(
    source: str | os.PathLike | bytes,
    *,
    name: str | None = None,
    description: str | None = None,
    license_: str | None = None,
    date_published: str | None = None,
) -> bytes:
    """Create a capsule from a workspace file holding a provenance block.

    Args:
        source: Path or bytes of the HDF5 workspace.
        name: Capsule name.
        description: Capsule description.
        license_: Licence statement; a neutral text is used when None.
        date_published: ISO 8601 date (defaults to now).

    Returns:
        The capsule bytes.

    Raises:
        CapsuleError: If the workspace has no (valid) provenance block.
    """
    import h5py  # pylint: disable=import-outside-toplevel

    workspace = _read_source(source)
    try:
        with h5py.File(io.BytesIO(workspace), "r") as h5file:
            block = read_block(h5file)
    except (OSError, ProvenanceFormatError) as exc:
        raise CapsuleError(f"Invalid workspace: {exc}") from exc
    if block is None:
        raise CapsuleError("The workspace has no provenance block")
    ledger, locators = block
    options = {
        key: value
        for key, value in (
            ("name", name),
            ("description", description),
            ("license_", license_),
            ("date_published", date_published),
        )
        if value is not None
    }
    manifest = build_manifest(
        ledger,
        locators,
        workspace_sha256=sha256_digest(workspace),
        workspace_size=len(workspace),
        **options,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry, data in (
            (MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1)),
            (WORKSPACE_NAME, workspace),
        ):
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _check_entry(info: zipfile.ZipInfo, seen: set[str]) -> None:
    name = info.filename
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ".." in name.split("/")
        or (len(name) > 1 and name[1] == ":")
    ):
        raise CapsuleError(f"Unsafe entry name: {name!r}")
    if name in seen:
        raise CapsuleError(f"Duplicate entry: {name}")
    seen.add(name)
    if name not in _LIMITS:
        raise CapsuleError(f"Unexpected entry: {name}")
    if info.flag_bits & 0x1:
        raise CapsuleError(f"Encrypted entry: {name}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise CapsuleError(f"Link entry: {name}")
    if info.file_size > _LIMITS[name]:
        raise CapsuleError(f"Entry too large: {name}")
    if (
        info.compress_type != zipfile.ZIP_STORED
        and info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1)
    ):
        raise CapsuleError(f"Compression ratio too high: {name}")


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    limit = _LIMITS[info.filename]
    with archive.open(info) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or len(data) != info.file_size:
        raise CapsuleError(f"Entry size does not match its header: {info.filename}")
    return data


def read_capsule(source: str | os.PathLike | bytes) -> Capsule:
    """Read, check and validate a capsule without extracting it to disk.

    Raises:
        CapsuleError: If the archive is unsafe, incomplete or inconsistent.
        ManifestError: If the manifest is invalid.
    """
    data = _read_source(source)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CapsuleError(f"Not a ZIP archive: {exc}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise CapsuleError("Too many entries")
        seen: set[str] = set()
        for info in infos:
            _check_entry(info, seen)
        missing = set(_LIMITS) - seen
        if missing:
            raise CapsuleError(f"Missing entries: {', '.join(sorted(missing))}")
        contents = {info.filename: _read_entry(archive, info) for info in infos}
    try:
        manifest = json.loads(contents[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"Invalid manifest JSON: {exc}") from exc
    validate_manifest(manifest)
    workspace = contents[WORKSPACE_NAME]
    entity = next(e for e in manifest["@graph"] if e["@id"] == WORKSPACE_NAME)
    if entity["sha256"] != sha256_digest(workspace).split(":", 1)[1] or entity[
        "contentSize"
    ] != str(len(workspace)):
        raise CapsuleError(f"{WORKSPACE_NAME} does not match the manifest")
    return Capsule(manifest, workspace)
