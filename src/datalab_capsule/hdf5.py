# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Provenance block of DataLab HDF5 workspaces, read and written with h5py alone.

Layout:

- group ``/DataLab_Provenance`` with attribute ``schema_version = 1``;
- dataset ``ledger_json``: UTF-8 ledger;
- dataset ``locators_json``: UTF-8 mapping ``state_id -> {"kind", "path"}`` for
  every state present in the file;
- optional dataset ``capsule_origin_json``: UTF-8
  ``{"fingerprint", "name", "attachments"}`` of the capsule the workspace was
  opened from, so that a revision exported after the workspace was saved and
  opened again still names its parent (readers without it ignore it).

Readers validate the block before anything else and raise
:class:`ProvenanceFormatError`, never ``KeyError``. External links, soft links and
virtual datasets are refused; sizes are checked before reading.
"""

from __future__ import annotations

import json
import numbers
import re
from collections.abc import Mapping
from typing import Any

from datalab_capsule.integrity import (
    MAX_JSON_DEPTH,
    ProvenanceError,
    image_fingerprint,
    load_json,
    signal_fingerprint_record,
    value_digest,
)
from datalab_capsule.ledger import Ledger, LedgerError

__all__ = [
    "BLOCK_GROUP",
    "BLOCK_SCHEMA_VERSION",
    "CAPSULE_ORIGIN_DATASET",
    "MAX_IMAGE_BYTES",
    "MAX_JSON_BYTES",
    "MAX_SIGNAL_BYTES",
    "PANEL_ROOTS",
    "ProvenanceFormatError",
    "build_locators",
    "check_capsule_origin",
    "has_block",
    "load_block",
    "load_ledger",
    "locate_current_states",
    "locate_fingerprints",
    "read_block",
    "read_capsule_origin",
    "read_image",
    "read_roi",
    "read_signal",
    "save_ledger",
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
#: Largest image ``data`` dataset read for fingerprinting (bytes).
MAX_IMAGE_BYTES = 2 * 1024**3
#: Metadata key of an object's region of interest (DataLab and Sigima).
ROI_METADATA_KEY = "_roi_"
#: Largest number of HDF5 nodes decoded for one ROI definition.
MAX_ROI_NODES = 10_000
#: Dataset of the block naming the capsule the workspace was opened from.
CAPSULE_ORIGIN_DATASET = "capsule_origin_json"
#: Largest number of attachment names accepted in a capsule origin.
MAX_ORIGIN_ATTACHMENTS = 1000
_MANIFEST_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


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
    # A scalar has size 1 (its type may be a large array); a null dataspace None
    size = (dataset.size or 0) * dataset.dtype.itemsize
    if size > max_bytes or dataset.id.get_storage_size() > max_bytes:
        raise ProvenanceFormatError(f"Dataset too large: {dataset.name}")


def _scalar_attr(node: Any, name: str, default: Any = None) -> Any:
    """Return the attribute *name* of *node*, refusing an array."""
    value = node.attrs.get(name, default)
    # NumPy values have ndim (0 for a scalar); Python values have none
    if getattr(value, "ndim", 0) != 0:
        raise ProvenanceFormatError(f"Not a scalar attribute: {node.name}@{name}")
    return value


def _read_text(group: Any, name: str) -> str:
    h5py = _h5py()
    if name not in group:
        raise ProvenanceFormatError(f"Missing dataset: {group.name}/{name}")
    _check_link(group, name)
    dataset = group[name]
    if (
        not isinstance(dataset, h5py.Dataset)
        or dataset.shape != ()
        or h5py.check_string_dtype(dataset.dtype) is None
    ):
        raise ProvenanceFormatError(f"Not a scalar string dataset: {dataset.name}")
    _check_dataset(dataset, MAX_JSON_BYTES)
    value = dataset[()]
    # The storage size of a variable-length string only counts its pointer.
    size = len(value) if isinstance(value, bytes) else len(str(value).encode())
    if size > MAX_JSON_BYTES:
        raise ProvenanceFormatError(f"Dataset too large: {dataset.name}")
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


def check_capsule_origin(origin: Any) -> dict[str, Any]:
    """Return a checked copy of a capsule origin.

    Args:
        origin: ``{"fingerprint", "name", "attachments"}`` (see
         :func:`datalab_capsule.exchange.capsule_origin`).

    Raises:
        ProvenanceFormatError: If *origin* does not have that form.
    """
    if not isinstance(origin, Mapping) or set(origin) != {
        "fingerprint",
        "name",
        "attachments",
    }:
        raise ProvenanceFormatError("Invalid capsule origin")
    fingerprint, name = origin["fingerprint"], origin["name"]
    attachments = origin["attachments"]
    if not isinstance(fingerprint, str) or not _MANIFEST_FINGERPRINT.match(fingerprint):
        raise ProvenanceFormatError("Invalid capsule origin fingerprint")
    if name is not None and not isinstance(name, str):
        raise ProvenanceFormatError("Invalid capsule origin name")
    if (
        not isinstance(attachments, (list, tuple))
        or len(attachments) > MAX_ORIGIN_ATTACHMENTS
        or not all(isinstance(item, str) for item in attachments)
    ):
        raise ProvenanceFormatError("Invalid capsule origin attachments")
    return {"fingerprint": fingerprint, "name": name, "attachments": list(attachments)}


def write_block(
    h5file: Any,
    ledger: Ledger,
    locators: Mapping[str, Mapping[str, str]],
    capsule_origin: Mapping[str, Any] | None = None,
) -> None:
    """Write (or replace) the provenance block of an open, writable HDF5 file.

    Args:
        h5file: Open, writable HDF5 file.
        ledger: Ledger to write.
        locators: ``state_id -> {"kind", "path"}`` of the states in the file.
        capsule_origin: Capsule the workspace was opened from, or None.
    """
    h5py = _h5py()
    origin = None if capsule_origin is None else check_capsule_origin(capsule_origin)
    if BLOCK_GROUP in h5file:
        del h5file[BLOCK_GROUP]
    group = h5file.create_group(BLOCK_GROUP)
    group.attrs["schema_version"] = BLOCK_SCHEMA_VERSION
    dtype = h5py.string_dtype("utf-8")
    group.create_dataset("ledger_json", data=ledger.to_json(), dtype=dtype)
    locators_text = json.dumps(dict(locators), ensure_ascii=False, sort_keys=True)
    group.create_dataset("locators_json", data=locators_text, dtype=dtype)
    if origin is not None:
        origin_text = json.dumps(origin, ensure_ascii=False, sort_keys=True)
        group.create_dataset(CAPSULE_ORIGIN_DATASET, data=origin_text, dtype=dtype)


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
    version = group.attrs.get("schema_version")
    if not isinstance(version, numbers.Integral) or version != BLOCK_SCHEMA_VERSION:
        raise ProvenanceFormatError("Unsupported provenance block schema_version")
    try:
        ledger = Ledger.from_json(_read_text(group, "ledger_json"))
    except LedgerError as exc:
        raise ProvenanceFormatError(str(exc)) from exc
    try:
        locators = load_json(_read_text(group, "locators_json"))
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


def read_capsule_origin(h5file: Any) -> dict[str, Any] | None:
    """Read the capsule the workspace was opened from, if the block names one.

    Returns:
        ``{"fingerprint", "name", "attachments"}``, or None.

    Raises:
        ProvenanceFormatError: If the origin is present but invalid.
    """
    h5py = _h5py()
    if BLOCK_GROUP not in h5file:
        return None
    _check_link(h5file, BLOCK_GROUP)
    group = h5file[BLOCK_GROUP]
    if not isinstance(group, h5py.Group):
        raise ProvenanceFormatError(f"{BLOCK_GROUP} must be a group")
    if CAPSULE_ORIGIN_DATASET not in group:
        return None
    try:
        origin = load_json(_read_text(group, CAPSULE_ORIGIN_DATASET))
    except ValueError as exc:
        if isinstance(exc, ProvenanceFormatError):
            raise
        raise ProvenanceFormatError(f"Invalid capsule origin: {exc}") from exc
    return check_capsule_origin(origin)


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
        if not isinstance(h5file[root], h5py.Group):
            raise ProvenanceFormatError(f"{root} must be a group")
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
                object_uuid = _object_uuid(obj)
                if object_uuid is None:
                    continue
                if object_uuid in index:
                    raise ProvenanceFormatError(f"Duplicate object UUID {object_uuid}")
                index[object_uuid] = {"kind": kind, "path": obj.name}
    return index


def _object_uuid(obj: Any) -> str | None:
    """Return the ``__uuid`` attribute of an object group's metadata, or None.

    Raises:
        ProvenanceFormatError: If the metadata are a link, not a group, or the
         UUID is not valid UTF-8.
    """
    if "metadata" not in obj:
        return None
    _check_link(obj, "metadata")
    metadata = obj["metadata"]
    if not isinstance(metadata, _h5py().Group):
        raise ProvenanceFormatError(f"Invalid metadata: {obj.name}")
    object_uuid = metadata.attrs.get("__uuid")
    if isinstance(object_uuid, bytes):
        try:
            object_uuid = object_uuid.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvenanceFormatError(f"Invalid UTF-8 in {obj.name} UUID") from exc
    if not isinstance(object_uuid, str) or not object_uuid:
        return None
    return object_uuid


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


def _object_node(h5file: Any, path: str) -> Any:
    h5py = _h5py()
    _check_path(path)
    node: Any = h5file
    for part in path.strip("/").split("/"):
        if not isinstance(node, h5py.Group):
            raise ProvenanceFormatError(f"Not an object group: {path}")
        if part not in node:
            raise ProvenanceFormatError(f"Missing object: {path}")
        _check_link(node, part)
        node = node[part]
    if not isinstance(node, h5py.Group):
        raise ProvenanceFormatError(f"Not an object group: {path}")
    return node


def _text_attr(node: Any, name: str) -> str:
    value = node.attrs.get(name, "")
    if not isinstance(value, bytes):
        return str(value)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceFormatError(f"Invalid UTF-8 in {node.name}@{name}") from exc


def _check_numeric(dataset: Any, path: str) -> None:
    """Refuse object, string or compound data where numbers are expected."""
    if dataset.dtype.kind not in "biufc":
        raise ProvenanceFormatError(f"Not numeric data: {path}/{dataset.name}")


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
    node = _object_node(h5file, path)
    if "xydata" not in node:
        raise ProvenanceFormatError(f"Not a signal object: {path}")
    _check_link(node, "xydata")
    dataset = node["xydata"]
    if not isinstance(dataset, h5py.Dataset) or dataset.ndim != 2:
        raise ProvenanceFormatError(f"Invalid xydata: {path}")
    _check_dataset(dataset, MAX_SIGNAL_BYTES)
    _check_numeric(dataset, path)
    if dataset.shape[0] not in (2, 3, 4):
        raise ProvenanceFormatError(f"Invalid xydata rows: {path}")
    data = dataset[()]
    rows = list(data) + [None] * (4 - data.shape[0])
    return {
        "x": rows[0],
        "y": rows[1],
        "dx": rows[2],
        "dy": rows[3],
        "xunit": _text_attr(node, "xunit"),
        "yunit": _text_attr(node, "yunit"),
    }


def read_image(h5file: Any, path: str) -> dict[str, Any]:
    """Read the data, coordinates and units of an image object with h5py alone.

    Returns:
        Keyword arguments of :func:`~datalab_capsule.integrity.image_fingerprint`
        plus ``data``.

    Raises:
        ProvenanceFormatError: If the object is missing, linked, virtual, too
         large or malformed.
    """
    h5py = _h5py()
    node = _object_node(h5file, path)
    arrays = {}
    for name in ("data", "xcoords", "ycoords"):
        if name not in node:
            if name == "data":
                raise ProvenanceFormatError(f"Not an image object: {path}")
            arrays[name] = None
            continue
        _check_link(node, name)
        dataset = node[name]
        if not isinstance(dataset, h5py.Dataset):
            raise ProvenanceFormatError(f"Invalid {name}: {path}")
        _check_dataset(dataset, MAX_IMAGE_BYTES)
        _check_numeric(dataset, path)
        arrays[name] = dataset[()]
    if arrays["data"].ndim != 2:
        raise ProvenanceFormatError(f"Invalid image data: {path}")
    result: dict[str, Any] = {
        "data": arrays["data"],
        "xunit": _text_attr(node, "xunit"),
        "yunit": _text_attr(node, "yunit"),
        "zunit": _text_attr(node, "zunit"),
    }
    if bool(_scalar_attr(node, "is_uniform_coords", True)):
        for name, default in (("x0", 0.0), ("y0", 0.0), ("dx", 1.0), ("dy", 1.0)):
            try:
                result[name] = float(node.attrs.get(name, default))
            except (TypeError, ValueError) as exc:
                raise ProvenanceFormatError(f"Invalid {name}: {path}") from exc
    else:
        if arrays["xcoords"] is None or arrays["ycoords"] is None:
            raise ProvenanceFormatError(f"Missing non-uniform coordinates: {path}")
        result["xcoords"] = arrays["xcoords"]
        result["ycoords"] = arrays["ycoords"]
    return result


def _decode_metadata(node: Any, budget: list[int], depth: int = 1) -> Any:
    """Decode a metadata value serialised by guidata (dicts, sequences, arrays).

    Groups carry ``__dict`` (mapping: scalar attributes plus child nodes) or
    ``__seq`` (``__seq<i>`` children or attributes); datasets hold arrays.
    *budget* bounds the number of decoded nodes, :data:`MAX_JSON_DEPTH` their
    nesting.
    """
    h5py = _h5py()
    budget[0] -= 1
    if budget[0] < 0:
        raise ProvenanceFormatError(f"Metadata too large: {node.name}")
    if depth > MAX_JSON_DEPTH:
        raise ProvenanceFormatError(f"Metadata nested too deeply: {node.name}")
    if isinstance(node, h5py.Dataset):
        _check_dataset(node, MAX_JSON_BYTES)
        return node[()]
    if "__seq" in node.attrs:
        items = []
        for index in range(int(_scalar_attr(node, "__seq"))):
            name = f"__seq{index}"
            if name in node:
                _check_link(node, name)
                items.append(_decode_metadata(node[name], budget, depth + 1))
            elif name in node.attrs:
                items.append(node.attrs[name])
            else:
                raise ProvenanceFormatError(f"Missing item {name}: {node.name}")
        return items
    if "__dict" not in node.attrs:
        raise ProvenanceFormatError(f"Unknown metadata node: {node.name}")
    value = {k: v for k, v in node.attrs.items() if k != "__dict"}
    for name in node:
        _check_link(node, name)
        value[name] = _decode_metadata(node[name], budget, depth + 1)
    return value


def read_roi(h5file: Any, path: str) -> Any:
    """Read the ROI definition of an object (``metadata/_roi_``), or None.

    Raises:
        ProvenanceFormatError: If the object or its ROI is malformed.
    """
    node = _object_node(h5file, path)
    if "metadata" not in node:
        return None
    _check_link(node, "metadata")
    metadata = node["metadata"]
    if not isinstance(metadata, _h5py().Group):
        raise ProvenanceFormatError(f"Invalid metadata: {path}")
    if ROI_METADATA_KEY not in metadata:
        return None
    _check_link(metadata, ROI_METADATA_KEY)
    try:
        return _decode_metadata(metadata[ROI_METADATA_KEY], [MAX_ROI_NODES])
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ProvenanceError):
            raise
        # e.g. a sequence length that is not a number
        raise ProvenanceFormatError(f"Invalid ROI of {path}: {exc}") from exc


def locate_fingerprints(
    h5file: Any, locators: Mapping[str, Mapping[str, str]]
) -> dict[str, str | None]:
    """Recompute the fingerprint of each located signal or image.

    Returns:
        ``state_id -> fingerprint value``.

    Raises:
        ProvenanceFormatError: If a located object is malformed.
    """
    result: dict[str, str | None] = {}
    for state_id, locator in locators.items():
        if locator["kind"] == "image":
            image = read_image(h5file, locator["path"])
            try:
                result[state_id] = image_fingerprint(image.pop("data"), **image)
            except (TypeError, ValueError) as exc:
                raise ProvenanceFormatError(
                    f"Invalid image {locator['path']}: {exc}"
                ) from exc
            continue
        signal = read_signal(h5file, locator["path"])
        try:
            record = signal_fingerprint_record(
                signal["x"],
                signal["y"],
                signal["dx"],
                signal["dy"],
                signal["xunit"],
                signal["yunit"],
            )
        except (TypeError, ValueError) as exc:
            raise ProvenanceFormatError(
                f"Invalid signal {locator['path']}: {exc}"
            ) from exc
        result[state_id] = record["value"]
    return result


def locate_current_states(h5file: Any, ledger: Ledger) -> dict[str, dict[str, str]]:
    """Locate the latest state of each ledger object whose saved data match it.

    A state matches when both its data fingerprint and its ROI (absent or with
    the same definition) match the saved object. Objects changed since their
    latest recorded state, objects of another kind and states without
    fingerprint get no locator.

    Returns:
        ``state_id -> {"kind", "path"}``.
    """
    index = scan_object_index(h5file)
    candidates: dict[str, dict[str, str]] = {}
    for object_uuid in ledger.object_uuids():
        state = ledger.latest_state(object_uuid)
        location = index.get(object_uuid)
        if (
            location is not None
            and state["fingerprint"] is not None
            and location["kind"] == state["kind"]
        ):
            candidates[state["state_id"]] = location
    altered = _altered_states(h5file, ledger, candidates)
    return {k: v for k, v in candidates.items() if k not in altered}


def _altered_states(
    h5file: Any, ledger: Ledger, locators: Mapping[str, Mapping[str, str]]
) -> set[str]:
    """Return the located states whose saved data or ROI no longer match."""
    fingerprints = locate_fingerprints(h5file, locators)
    altered = set()
    for state_id, locator in locators.items():
        state = ledger.states[state_id]
        roi = read_roi(h5file, locator["path"])
        try:
            roi_digest = None if roi is None else value_digest(roi)
        except (TypeError, ValueError) as exc:
            # e.g. text that is not UTF-8, or a value JSON cannot represent
            raise ProvenanceFormatError(
                f"Invalid ROI of {locator['path']}: {exc}"
            ) from exc
        recorded_roi = (state.get("roi") or {}).get("digest")
        if (
            state["fingerprint"] is None
            or fingerprints[state_id] != state["fingerprint"]["value"]
            or roi_digest != recorded_roi
        ):
            altered.add(state_id)
    return altered


def save_ledger(
    h5file: Any, ledger: Ledger, capsule_origin: Mapping[str, Any] | None = None
) -> None:
    """Write the provenance block of a workspace file, after its panels.

    Args:
        h5file: Open, writable HDF5 file holding the saved panels.
        ledger: Ledger to write.
        capsule_origin: Capsule the workspace was opened from, or None.
    """
    write_block(h5file, ledger, locate_current_states(h5file, ledger), capsule_origin)


def load_block(
    h5file: Any,
) -> tuple[Ledger, dict[str, dict[str, str]], dict[str, str]] | None:
    """Read the provenance block once: ledger, locators and state statuses.

    See :func:`load_ledger` for the statuses.

    Returns:
        ``(ledger, locators, state_status)``, or None when the file has no
        block.

    Raises:
        ProvenanceFormatError: If the block or a located object is invalid.
    """
    block = read_block(h5file)
    if block is None:
        return None
    ledger, locators = block
    for state_id, locator in locators.items():
        expected = ledger.states[state_id]["object_uuid"]
        if _object_uuid(_object_node(h5file, locator["path"])) != expected:
            raise ProvenanceFormatError(
                f"Locator of {state_id} points to another object: {locator['path']}"
            )
    altered = _altered_states(h5file, ledger, locators)
    status: dict[str, str] = {}
    for state_id in ledger.states:
        if state_id not in locators:
            status[state_id] = "unavailable"
        elif state_id in altered:
            status[state_id] = "altered"
    return ledger, locators, status


def load_ledger(h5file: Any) -> tuple[Ledger, dict[str, str]] | None:
    """Read the provenance block and derive the status of each state.

    A located state whose data or ROI no longer match is ``altered``; a state
    without locator is ``unavailable``.

    Returns:
        ``(ledger, state_status)``, or None when the file has no block.

    Raises:
        ProvenanceFormatError: If the block or a located object is invalid.
    """
    block = load_block(h5file)
    if block is None:
        return None
    ledger, _locators, status = block
    return ledger, status
