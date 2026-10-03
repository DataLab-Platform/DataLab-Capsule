# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Provenance block of DataLab HDF5 workspaces, read and written with h5py alone.

Layout:

- group ``/DataLab_Provenance`` with attribute ``schema_version = 1``;
- dataset ``ledger_json``: UTF-8 ledger;
- dataset ``locators_json``: UTF-8 mapping ``state_id -> {"kind", "path"}`` for
  every state present in the file.

Readers validate the block before anything else and raise
:class:`ProvenanceFormatError`, never ``KeyError``. External links, soft links and
virtual datasets are refused; sizes are checked before reading.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from datalab_capsule.integrity import ProvenanceError, signal_fingerprint
from datalab_capsule.ledger import Ledger, LedgerError

__all__ = [
    "BLOCK_GROUP",
    "BLOCK_SCHEMA_VERSION",
    "MAX_JSON_BYTES",
    "MAX_SIGNAL_BYTES",
    "PANEL_ROOTS",
    "ProvenanceFormatError",
    "build_locators",
    "has_block",
    "locate_fingerprints",
    "read_block",
    "read_signal",
    "scan_object_index",
    "write_block",
]

BLOCK_GROUP = "DataLab_Provenance"
BLOCK_SCHEMA_VERSION = 1
PANEL_ROOTS = {"signal": "DataLab_Sig", "image": "DataLab_Ima"}
#: Largest ledger or locator JSON accepted when reading (bytes).
MAX_JSON_BYTES = 64 * 1024 * 1024
#: Largest signal ``xydata`` dataset read for fingerprinting (bytes).
MAX_SIGNAL_BYTES = 2 * 1024**3


class ProvenanceFormatError(ProvenanceError, ValueError):
    """Raised when a provenance block or a located object cannot be trusted."""


def _h5py():
    import h5py  # pylint: disable=import-outside-toplevel

    return h5py


def _check_link(group: Any, name: str) -> None:
    h5py = _h5py()
    link = group.get(name, getlink=True)
    if isinstance(link, (h5py.ExternalLink, h5py.SoftLink)):
        raise ProvenanceFormatError(f"Link refused: {group.name}/{name}")


def _check_dataset(dataset: Any, max_bytes: int) -> None:
    if dataset.is_virtual:
        raise ProvenanceFormatError(f"Virtual dataset refused: {dataset.name}")
    size = dataset.size * dataset.dtype.itemsize if dataset.shape else 0
    if size > max_bytes or dataset.id.get_storage_size() > max_bytes:
        raise ProvenanceFormatError(f"Dataset too large: {dataset.name}")


def _read_text(group: Any, name: str) -> str:
    h5py = _h5py()
    if name not in group:
        raise ProvenanceFormatError(f"Missing dataset: {group.name}/{name}")
    _check_link(group, name)
    dataset = group[name]
    if not isinstance(dataset, h5py.Dataset) or dataset.shape != ():
        raise ProvenanceFormatError(f"Not a scalar string dataset: {dataset.name}")
    if dataset.id.get_storage_size() > MAX_JSON_BYTES:
        raise ProvenanceFormatError(f"Dataset too large: {dataset.name}")
    value = dataset[()]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvenanceFormatError(f"Invalid UTF-8 in {dataset.name}") from exc
    if not isinstance(value, str):
        raise ProvenanceFormatError(f"Not a string dataset: {dataset.name}")
    return value


def has_block(h5file: Any) -> bool:
    """Return True if the file holds a provenance block."""
    return BLOCK_GROUP in h5file


def write_block(
    h5file: Any, ledger: Ledger, locators: Mapping[str, Mapping[str, str]]
) -> None:
    """Write (or replace) the provenance block of an open, writable HDF5 file."""
    h5py = _h5py()
    if BLOCK_GROUP in h5file:
        del h5file[BLOCK_GROUP]
    group = h5file.create_group(BLOCK_GROUP)
    group.attrs["schema_version"] = BLOCK_SCHEMA_VERSION
    dtype = h5py.string_dtype("utf-8")
    group.create_dataset("ledger_json", data=ledger.to_json(), dtype=dtype)
    locators_text = json.dumps(dict(locators), ensure_ascii=False, sort_keys=True)
    group.create_dataset("locators_json", data=locators_text, dtype=dtype)


def _check_path(path: Any) -> None:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ProvenanceFormatError(f"Invalid locator path: {path!r}")
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] not in PANEL_ROOTS.values() or ".." in parts:
        raise ProvenanceFormatError(f"Locator outside known object paths: {path!r}")


def read_block(h5file: Any) -> tuple[Ledger, dict[str, dict[str, str]]] | None:
    """Read and validate the provenance block.

    Returns:
        ``(ledger, locators)``, or None when the file has no block.

    Raises:
        ProvenanceFormatError: If the block is present but invalid.
    """
    h5py = _h5py()
    if BLOCK_GROUP not in h5file:
        return None
    _check_link(h5file, BLOCK_GROUP)
    group = h5file[BLOCK_GROUP]
    if not isinstance(group, h5py.Group):
        raise ProvenanceFormatError(f"{BLOCK_GROUP} must be a group")
    if group.attrs.get("schema_version") != BLOCK_SCHEMA_VERSION:
        raise ProvenanceFormatError("Unsupported provenance block schema_version")
    try:
        ledger = Ledger.from_json(_read_text(group, "ledger_json"))
    except LedgerError as exc:
        raise ProvenanceFormatError(str(exc)) from exc
    try:
        locators = json.loads(_read_text(group, "locators_json"))
    except ValueError as exc:
        raise ProvenanceFormatError(f"Invalid locators: {exc}") from exc
    if not isinstance(locators, dict):
        raise ProvenanceFormatError("Locators must be a JSON object")
    for state_id, locator in locators.items():
        if state_id not in ledger.states:
            raise ProvenanceFormatError(f"Locator for unknown state {state_id}")
        if not isinstance(locator, dict) or set(locator) != {"kind", "path"}:
            raise ProvenanceFormatError(f"Invalid locator for {state_id}")
        if locator["kind"] != ledger.states[state_id]["kind"]:
            raise ProvenanceFormatError(f"Locator kind mismatch for {state_id}")
        _check_path(locator["path"])
    return ledger, locators


def scan_object_index(h5file: Any) -> dict[str, dict[str, str]]:
    """Map each object UUID found in the panel groups to its kind and path.

    Objects are ``/<panel root>/<group>/<object>`` groups whose ``metadata``
    subgroup carries the ``__uuid`` attribute.

    Raises:
        ProvenanceFormatError: On links or duplicate UUIDs.
    """
    h5py = _h5py()
    index: dict[str, dict[str, str]] = {}
    for kind, root in PANEL_ROOTS.items():
        if root not in h5file:
            continue
        _check_link(h5file, root)
        for group_name in h5file[root]:
            _check_link(h5file[root], group_name)
            group = h5file[root][group_name]
            if not isinstance(group, h5py.Group):
                continue
            for obj_name in group:
                _check_link(group, obj_name)
                obj = group[obj_name]
                if not isinstance(obj, h5py.Group) or "metadata" not in obj:
                    continue
                object_uuid = obj["metadata"].attrs.get("__uuid")
                if isinstance(object_uuid, bytes):
                    object_uuid = object_uuid.decode("utf-8")
                if not isinstance(object_uuid, str) or not object_uuid:
                    continue
                if object_uuid in index:
                    raise ProvenanceFormatError(f"Duplicate object UUID {object_uuid}")
                index[object_uuid] = {"kind": kind, "path": obj.name}
    return index


def build_locators(
    h5file: Any, current_states: Mapping[str, str], kinds: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """Locate the current state of each object written in the file.

    Args:
        h5file: Open HDF5 file, after the panels were written.
        current_states: ``object_uuid -> state_id`` of the saved content.
        kinds: ``state_id -> kind``.

    Returns:
        ``state_id -> {"kind", "path"}``.
    """
    index = scan_object_index(h5file)
    return {
        state_id: index[object_uuid]
        for object_uuid, state_id in current_states.items()
        if object_uuid in index and index[object_uuid]["kind"] == kinds[state_id]
    }


def read_signal(h5file: Any, path: str) -> dict[str, Any]:
    """Read the rows and units of a signal object with h5py alone.

    Returns:
        ``{"x", "y", "dx", "dy", "xunit", "yunit"}`` (uncertainty rows are None
        when absent).

    Raises:
        ProvenanceFormatError: If the object is missing, linked, virtual, too
         large or malformed.
    """
    h5py = _h5py()
    _check_path(path)
    parts = path.strip("/").split("/")
    node: Any = h5file
    for part in parts:
        if part not in node:
            raise ProvenanceFormatError(f"Missing object: {path}")
        _check_link(node, part)
        node = node[part]
    if "xydata" not in node:
        raise ProvenanceFormatError(f"Not a signal object: {path}")
    _check_link(node, "xydata")
    dataset = node["xydata"]
    if not isinstance(dataset, h5py.Dataset) or dataset.ndim != 2:
        raise ProvenanceFormatError(f"Invalid xydata: {path}")
    _check_dataset(dataset, MAX_SIGNAL_BYTES)
    if dataset.shape[0] not in (2, 3, 4):
        raise ProvenanceFormatError(f"Invalid xydata rows: {path}")
    data = dataset[()]
    rows = list(data) + [None] * (4 - data.shape[0])

    def unit(name: str) -> str:
        value = node.attrs.get(name, "")
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    return {
        "x": rows[0],
        "y": rows[1],
        "dx": rows[2],
        "dy": rows[3],
        "xunit": unit("xunit"),
        "yunit": unit("yunit"),
    }


def locate_fingerprints(
    h5file: Any, locators: Mapping[str, Mapping[str, str]]
) -> dict[str, str | None]:
    """Recompute the ``datalab-signal-v1`` fingerprint of each located signal.

    Returns:
        ``state_id -> fingerprint value`` (None for non-signal states).
    """
    result: dict[str, str | None] = {}
    for state_id, locator in locators.items():
        if locator["kind"] != "signal":
            result[state_id] = None
            continue
        signal = read_signal(h5file, locator["path"])
        result[state_id] = signal_fingerprint(
            signal["x"],
            signal["y"],
            signal["dx"],
            signal["dy"],
            signal["xunit"],
            signal["yunit"],
        )
    return result
