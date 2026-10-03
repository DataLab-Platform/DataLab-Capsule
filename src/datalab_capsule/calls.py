# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Operation calls: the neutral description of one function call.

A call names an operation (identifier and contract version), its parameters as
plain JSON values and one binding per input role. Repeated roles appear once per
input, in call order. A call whose ``operation`` is ``None`` is *opaque*: the
operation has no qualified contract and the call is never replayed.

Binding kinds:

- ``{"state_id": ...}``: a concrete state (used by activities);
- ``{"object_uuids": [...]}``: an object selection (reserved);
- ``{"step": ..., "role": ...}``: the output of an upstream step (reserved).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from datalab_capsule.integrity import ProvenanceError

__all__ = [
    "BINDING_KINDS",
    "OPERATION_ID_PATTERN",
    "CallError",
    "ReservedBindingError",
    "is_opaque",
    "make_call",
    "validate_call",
]

OPERATION_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
BINDING_KINDS = ("state_id", "object_uuids", "step")
_IMPLEMENTED_BINDINGS = ("state_id",)


class CallError(ProvenanceError, ValueError):
    """Raised when an operation call is malformed."""


class ReservedBindingError(CallError):
    """Raised when a call uses a binding kind that is not implemented yet."""


def make_call(
    operation_id: str | None,
    contract_version: int | None,
    parameters: dict[str, Any] | None,
    inputs: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build and validate an operation call bound to states.

    Args:
        operation_id: Operation identifier, or None for an opaque call.
        contract_version: Contract version (required with *operation_id*).
        parameters: Encoded parameter values, or None if they could not be
         encoded.
        inputs: Ordered ``(role, state_id)`` pairs.

    Returns:
        JSON-compatible call.
    """
    operation = None
    if operation_id is not None:
        operation = {"id": operation_id, "contract_version": contract_version}
    call = {
        "operation": operation,
        "parameters": copy.deepcopy(parameters),
        "inputs": [
            {"role": role, "binding": {"state_id": state_id}}
            for role, state_id in inputs
        ],
    }
    validate_call(call)
    return call


def is_opaque(call: dict[str, Any]) -> bool:
    """Return True if *call* names no operation."""
    return call.get("operation") is None


def _check_binding(binding: Any, where: str) -> None:
    if not isinstance(binding, dict) or len(binding) == 0:
        raise CallError(f"{where}: binding must be a non-empty object")
    kinds = [kind for kind in BINDING_KINDS if kind in binding]
    if len(kinds) != 1:
        raise CallError(f"{where}: binding must hold exactly one of {BINDING_KINDS}")
    kind = kinds[0]
    if kind not in _IMPLEMENTED_BINDINGS:
        raise ReservedBindingError(f"{where}: binding kind {kind!r} is reserved")
    if set(binding) != {"state_id"} or not isinstance(binding["state_id"], str):
        raise CallError(f"{where}: state binding must be {{'state_id': <str>}}")


def validate_call(call: Any) -> None:
    """Validate the structure of an operation call.

    Raises:
        ReservedBindingError: If a reserved binding kind is used.
        CallError: If the call is malformed.
    """
    if not isinstance(call, dict) or set(call) != {"operation", "parameters", "inputs"}:
        raise CallError("A call holds exactly: operation, parameters, inputs")
    operation = call["operation"]
    if operation is not None:
        if not isinstance(operation, dict) or set(operation) != {
            "id",
            "contract_version",
        }:
            raise CallError("operation must be null or {id, contract_version}")
        if not isinstance(operation["id"], str) or not OPERATION_ID_PATTERN.match(
            operation["id"]
        ):
            raise CallError(f"Invalid operation identifier: {operation['id']!r}")
        version = operation["contract_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise CallError("contract_version must be an integer >= 1")
    parameters = call["parameters"]
    if parameters is not None and not isinstance(parameters, dict):
        raise CallError("parameters must be an object or null")
    inputs = call["inputs"]
    if not isinstance(inputs, list):
        raise CallError("inputs must be a list")
    for index, item in enumerate(inputs):
        where = f"inputs[{index}]"
        if not isinstance(item, dict) or set(item) != {"role", "binding"}:
            raise CallError(f"{where}: an input holds exactly role and binding")
        if not isinstance(item["role"], str) or not item["role"]:
            raise CallError(f"{where}: role must be a non-empty string")
        _check_binding(item["binding"], where)
