# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Workspace capsules: ``.dlcapsule`` ZIP archives holding a workspace and its manifest.

A capsule holds ``ro-crate-metadata.json`` (see :mod:`datalab_capsule.manifest`)
and, when the workspace is included, ``workspace.h5`` (the DataLab HDF5 workspace,
provenance block included), both stored without compression. A capsule may
instead reference its workspace by an absolute IRI: the archive then holds the
manifest only, and the workspace is obtained through a resolver passed
explicitly by the caller; its SHA-256 and size are checked before use.

The reader never executes code, never accesses the network and refuses unsafe
archives before extracting anything: absolute paths, ``..``, drive letters,
backslashes, duplicate or unexpected entries, links, encrypted or compressed
entries (so no decompression bomb can be read), and entries over the limits
below.

Size limits form a :class:`SizePolicy`. The archive size is checked before the
archive is read; the size of each entry is checked against its header before
extraction and against the bytes actually extracted. Each edition can pass its
own policy to :func:`read_capsule`. :data:`DEFAULT_SIZE_POLICY` is an accepted
upper bound, not a tested capacity: the whole archive is held in memory.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from datalab_capsule.hdf5 import ProvenanceFormatError, read_block
from datalab_capsule.integrity import ProvenanceError, load_json, sha256_digest
from datalab_capsule.manifest import (
    ATTACHMENTS_DIR,
    MANIFEST_NAME,
    REFERENCED_KEY,
    WORKSPACE_NAME,
    ManifestError,
    attachment_entities,
    attachment_name,
    build_manifest,
    check_attachment_name,
    validate_manifest,
    workspace_entity,
)

__all__ = [
    "CAPSULE_SUFFIX",
    "DEFAULT_SIZE_POLICY",
    "MAX_ARCHIVE_BYTES",
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ENTRIES",
    "MAX_MANIFEST_BYTES",
    "MAX_WORKSPACE_BYTES",
    "Capsule",
    "CapsuleError",
    "SizePolicy",
    "create_from_hdf5",
    "read_capsule",
]

CAPSULE_SUFFIX = ".dlcapsule"
#: Largest number of entries accepted in an archive.
MAX_ENTRIES = 16
#: Largest manifest accepted by default (bytes, uncompressed).
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
#: Largest workspace accepted by default (bytes, uncompressed).
MAX_WORKSPACE_BYTES = 4 * 1024**3
#: Largest number of light attachments in a capsule.
MAX_ATTACHMENTS = 8
#: Largest attachment accepted by default (bytes): attachments are light files.
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
#: Largest archive accepted by default (bytes): every entry plus ZIP headers.
MAX_ARCHIVE_BYTES = (
    MAX_MANIFEST_BYTES
    + MAX_WORKSPACE_BYTES
    + MAX_ATTACHMENTS * MAX_ATTACHMENT_BYTES
    + 1024 * 1024
)
_ENTRY_NAMES = (MANIFEST_NAME, WORKSPACE_NAME)


class CapsuleError(ProvenanceError, ValueError):
    """Raised when a capsule cannot be created or is not safe or valid."""


@dataclasses.dataclass(frozen=True)
class SizePolicy:
    """Size limits applied when reading a capsule (bytes).

    Attributes:
        max_archive_bytes: Largest archive, checked before it is read.
        max_manifest_bytes: Largest manifest (uncompressed).
        max_workspace_bytes: Largest workspace (uncompressed).
        max_attachment_bytes: Largest attachment (uncompressed).
    """

    max_archive_bytes: int
    max_manifest_bytes: int
    max_workspace_bytes: int
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES

    def entry_limit(self, name: str) -> int:
        """Return the size limit of entry *name*."""
        if name == MANIFEST_NAME:
            return self.max_manifest_bytes
        if name.startswith(ATTACHMENTS_DIR):
            return self.max_attachment_bytes
        return self.max_workspace_bytes


#: Policy used when an edition does not pass its own.
DEFAULT_SIZE_POLICY = SizePolicy(
    max_archive_bytes=MAX_ARCHIVE_BYTES,
    max_manifest_bytes=MAX_MANIFEST_BYTES,
    max_workspace_bytes=MAX_WORKSPACE_BYTES,
)


@dataclasses.dataclass(frozen=True)
class Capsule:
    """A validated capsule.

    Attributes:
        manifest: RO-Crate manifest.
        workspace: Bytes of the workspace, to pass to the editions' open path, or
         None when it is referenced and no resolver was given.
        workspace_ref: IRI of a referenced workspace, or None when included.
        attachments: ``name -> bytes`` of the declared attachments, checked
         against the manifest. They are data for the user: nothing in this
         package opens, imports or runs them.
    """

    manifest: dict[str, Any]
    workspace: bytes | None
    workspace_ref: str | None = None
    attachments: dict[str, bytes] = dataclasses.field(default_factory=dict)


def _read_source(source: str | os.PathLike | bytes, limit: int | None = None) -> bytes:
    """Return the bytes of *source*, refusing more than *limit* before reading."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        if limit is not None and len(source) > limit:
            raise CapsuleError(f"Archive too large: {len(source)} > {limit} bytes")
        return bytes(source)
    with open(source, "rb") as file:
        if limit is None:
            return file.read()
        size = os.fstat(file.fileno()).st_size
        if size > limit:
            raise CapsuleError(f"Archive too large: {size} > {limit} bytes")
        data = file.read(size + 1)
    if len(data) > limit:
        raise CapsuleError(f"Archive too large: more than {limit} bytes")
    return data


def create_from_hdf5(
    source: str | os.PathLike | bytes,
    *,
    name: str | None = None,
    description: str | None = None,
    license_: str | None = None,
    date_published: str | None = None,
    revision_of: str | None = None,
    workspace_ref: str | None = None,
    attachments: Sequence[Mapping[str, Any]] = (),
) -> bytes:
    """Create a capsule from a workspace file holding a provenance block.

    Args:
        source: Path or bytes of the HDF5 workspace.
        name: Capsule name.
        description: Capsule description.
        license_: Licence statement; a neutral text is used when None.
        date_published: ISO 8601 date (defaults to now).
        revision_of: Manifest fingerprint of the parent revision, if any.
        workspace_ref: Absolute IRI where the workspace will be published. When
         given, the workspace is referenced, not included: the archive holds
         the manifest only, with the workspace's SHA-256 and size.
        attachments: Light files selected by the author, stored as
         ``attachments/<name>`` and declared in the manifest with their size and
         SHA-256: ``{"name", "data", "media_type", "description"}``
         (``description`` optional).

    Returns:
        The capsule bytes.

    Raises:
        CapsuleError: If the workspace has no (valid) provenance block, or the
         attachments are too many or too large.
        ManifestError: If a manifest field is invalid (parent revision,
         workspace IRI, attachment name, media type).
    """
    import h5py  # pylint: disable=import-outside-toplevel

    if len(attachments) > MAX_ATTACHMENTS:
        raise CapsuleError(f"Too many attachments (at most {MAX_ATTACHMENTS})")
    declared = []
    for item in attachments:
        data = bytes(item["data"])
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise CapsuleError(f"Attachment too large: {item['name']!r}")
        declared.append(
            {
                "name": item["name"],
                "size": len(data),
                "sha256": sha256_digest(data),
                "media_type": item.get("media_type") or "application/octet-stream",
                "description": item.get("description"),
                "data": data,
            }
        )

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
            ("revision_of", revision_of),
            ("workspace_ref", workspace_ref),
        )
        if value is not None
    }
    manifest = build_manifest(
        ledger,
        locators,
        workspace_sha256=sha256_digest(workspace),
        workspace_size=len(workspace),
        attachments=declared,
        **options,
    )
    entries = [(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))]
    if workspace_ref is None:
        entries.append((WORKSPACE_NAME, workspace))
    entries.extend((ATTACHMENTS_DIR + item["name"], item["data"]) for item in declared)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for entry, data in entries:
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            # Unix permissions, whatever the host: same bytes on every platform.
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _check_entry(info: zipfile.ZipInfo, seen: set[str], policy: SizePolicy) -> None:
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
    if name.startswith(ATTACHMENTS_DIR):
        try:
            check_attachment_name(name[len(ATTACHMENTS_DIR) :])
        except ManifestError as exc:
            raise CapsuleError(f"Unsafe entry name: {name!r}") from exc
    elif name not in _ENTRY_NAMES:
        raise CapsuleError(f"Unexpected entry: {name}")
    if info.flag_bits & 0x1:
        raise CapsuleError(f"Encrypted entry: {name}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise CapsuleError(f"Link entry: {name}")
    if info.compress_type != zipfile.ZIP_STORED:
        raise CapsuleError(f"Compressed entry: {name}")
    if info.file_size > policy.entry_limit(name):
        raise CapsuleError(f"Entry too large: {name}")


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    try:
        with archive.open(info) as stream:
            # The header size was checked against *limit*: never read past it.
            data = stream.read(info.file_size + 1)
    except (zipfile.BadZipFile, EOFError, ValueError) as exc:
        # Raised while reading, e.g. on a CRC mismatch, a truncated entry or a
        # local header name that cannot be decoded
        raise CapsuleError(f"Damaged entry {info.filename}: {exc}") from exc
    if len(data) > limit or len(data) != info.file_size:
        raise CapsuleError(f"Entry size does not match its header: {info.filename}")
    return data


def read_capsule(
    source: str | os.PathLike | bytes,
    policy: SizePolicy = DEFAULT_SIZE_POLICY,
    resolve_workspace: Callable[[str], bytes] | None = None,
) -> Capsule:
    """Read, check and validate a capsule without extracting it to disk.

    Args:
        source: Path or bytes of the capsule.
        policy: Size limits of the calling edition.
        resolve_workspace: ``IRI -> bytes`` used for a referenced workspace. It
         is the only way a referenced workspace is obtained: nothing is fetched
         by default. The bytes are checked against the manifest.

    Returns:
        The capsule; ``workspace`` is None for a referenced workspace without
        resolver.

    Raises:
        CapsuleError: If the archive is unsafe, too large, incomplete or
         inconsistent, or the workspace does not match the manifest.
        ManifestError: If the manifest is invalid.
    """
    data = _read_source(source, policy.max_archive_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        # ValueError: e.g. an entry name flagged UTF-8 that is not UTF-8
        raise CapsuleError(f"Not a ZIP archive: {exc}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise CapsuleError("Too many entries")
        seen: set[str] = set()
        for info in infos:
            _check_entry(info, seen, policy)
        missing = {MANIFEST_NAME} - seen
        if missing:
            raise CapsuleError(f"Missing entries: {', '.join(sorted(missing))}")
        contents = {
            info.filename: _read_entry(archive, info, policy.entry_limit(info.filename))
            for info in infos
        }
    try:
        manifest = load_json(contents[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"Invalid manifest JSON: {exc}") from exc
    validate_manifest(manifest)
    attachments = _checked_attachments(manifest, contents)
    entity = workspace_entity(manifest)
    if not entity.get(REFERENCED_KEY):
        if WORKSPACE_NAME not in contents:
            raise CapsuleError(f"Missing entries: {WORKSPACE_NAME}")
        return Capsule(
            manifest,
            _checked(entity, contents[WORKSPACE_NAME]),
            attachments=attachments,
        )
    if WORKSPACE_NAME in contents:
        raise CapsuleError(
            f"Unexpected entry for a referenced workspace: {WORKSPACE_NAME}"
        )
    ref = entity["@id"]
    if resolve_workspace is None:
        return Capsule(manifest, None, ref, attachments)
    workspace = resolve_workspace(ref)
    if len(workspace) > policy.max_workspace_bytes:
        raise CapsuleError(f"Referenced workspace too large: {ref}")
    return Capsule(manifest, _checked(entity, bytes(workspace)), ref, attachments)


def _checked_attachments(
    manifest: dict[str, Any], contents: Mapping[str, bytes]
) -> dict[str, bytes]:
    """Return the declared attachments, each matching its size and SHA-256.

    Raises:
        CapsuleError: If an attachment is missing, undeclared or altered.
    """
    declared = {entity["@id"]: entity for entity in attachment_entities(manifest)}
    if len(declared) > MAX_ATTACHMENTS:
        raise CapsuleError(f"Too many attachments (at most {MAX_ATTACHMENTS})")
    present = {name for name in contents if name.startswith(ATTACHMENTS_DIR)}
    if present - set(declared):
        raise CapsuleError(
            f"Undeclared attachments: {', '.join(sorted(present - set(declared)))}"
        )
    if set(declared) - present:
        raise CapsuleError(
            f"Missing attachments: {', '.join(sorted(set(declared) - present))}"
        )
    result = {}
    for entry, entity in declared.items():
        data = contents[entry]
        if entity["sha256"] != sha256_digest(data).split(":", 1)[1] or entity[
            "contentSize"
        ] != str(len(data)):
            raise CapsuleError(f"The attachment does not match the manifest: {entry}")
        result[attachment_name(entity)] = data
    return result


def _checked(entity: dict[str, Any], workspace: bytes) -> bytes:
    """Return *workspace* if its SHA-256 and size match the manifest."""
    if entity["sha256"] != sha256_digest(workspace).split(":", 1)[1] or entity[
        "contentSize"
    ] != str(len(workspace)):
        raise CapsuleError(
            f"The workspace does not match the manifest: {entity['@id']}"
        )
    return workspace
