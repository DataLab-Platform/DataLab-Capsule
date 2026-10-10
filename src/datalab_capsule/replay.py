# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Shared preparation of a recorded activity for replay.

Preparation needs no GUI selection and no History session. It resolves the
contract, finds the live input objects, checks preconditions and input states, and
rebuilds the parameters. Each application injects its own resolvers, so that both
editions refuse replays with the same eligibility codes. Nothing here imports an
application or Sigima, and nothing here executes a computation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from datalab_capsule.calls import is_opaque
from datalab_capsule.integrity import ProvenanceError
from datalab_capsule.ledger import Ledger

__all__ = [
    "IneligibleError",
    "Plan",
    "Refusal",
    "prepare_activity",
]


class IneligibleError(ProvenanceError):
    """Raised by injected resolvers to refuse a replay with an eligibility code.

    Args:
        code: Eligibility code (e.g. ``"unsupported_contract"``).
        message: Human-readable detail.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Plan:
    """A recorded activity ready for a verification or commit run.

    Attributes:
        activity: The recorded activity.
        contract: Contract object returned by the injected resolver.
        parameters: Rebuilt parameter set.
        inputs: Ordered ``(role, state_id, live object)`` triples.
        call_inputs: Objects to pass to the function, in role order, after the
         recorded context was applied (e.g. X alignment); the live objects are
         never modified.
    """

    activity: dict[str, Any]
    contract: Any
    parameters: Any
    inputs: tuple[tuple[str, str, Any], ...]
    call_inputs: tuple[Any, ...] = ()


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Why a recorded activity cannot be replayed.

    Attributes:
        activity: The recorded activity.
        restoration: ``opaque`` or ``replayable``.
        eligibility: Eligibility code.
        reason: Human-readable detail.
        input_statuses: ``{"role", "state_id", "status"}`` items checked so far.
    """

    activity: dict[str, Any]
    restoration: str
    eligibility: str
    reason: str
    input_statuses: tuple[dict[str, Any], ...] = ()


def prepare_activity(
    ledger: Ledger,
    activity_id: str,
    *,
    resolve_contract: Callable[[str, int], Any],
    find_object: Callable[[str], Any],
    observe_object: Callable[[Any], dict[str, Any]],
    check_preconditions: Callable[[Any, list[Any]], str | None],
    decode_parameters: Callable[[Any, dict[str, Any]], Any],
    state_status: Mapping[str, str] | None = None,
    apply_context: Callable[[Any, list[Any], dict[str, Any]], list[Any]] | None = None,
) -> Plan | Refusal:
    """Prepare a recorded activity for replay.

    Args:
        ledger: Ledger holding the activity.
        activity_id: Activity to prepare.
        resolve_contract: ``(operation_id, contract_version) -> contract``; raises
         :class:`IneligibleError` (``unsupported_operation`` or
         ``unsupported_contract``).
        find_object: ``object_uuid -> live object or None``.
        observe_object: ``live object -> state facts`` (fingerprint included).
        check_preconditions: ``(contract, objects) -> failing check or None``.
        decode_parameters: ``(contract, values) -> parameters``; raises
         :class:`IneligibleError` (``invalid_parameters``).
        state_status: Statuses derived at load (``altered``, ``unavailable``).
        apply_context: ``(contract, objects, recorded context) -> call inputs``;
         raises :class:`IneligibleError` (``unsupported_context``) when the
         recorded context cannot be applied. Defaults to the live objects.

    Returns:
        A :class:`Plan`, or a :class:`Refusal` with its eligibility code.

    Raises:
        KeyError: If the activity does not exist.
    """
    activity = ledger.activity(activity_id)
    call = activity["call"]
    status = state_status or {}

    def refuse(code: str, reason: str, inputs=(), restoration="replayable"):
        return Refusal(activity, restoration, code, reason, tuple(inputs))

    if is_opaque(call):
        return refuse(
            "unsupported_operation",
            "The call names no qualified operation",
            restoration="opaque",
        )
    operation = call["operation"]
    try:
        contract = resolve_contract(operation["id"], operation["contract_version"])
    except IneligibleError as exc:
        return refuse(exc.code, str(exc))
    checked: list[dict[str, Any]] = []
    objects: list[tuple[str, str, Any]] = []
    for item in call["inputs"]:
        state = ledger.states[item["binding"]["state_id"]]
        obj = find_object(state["object_uuid"])
        if obj is None:
            checked.append(
                {
                    "role": item["role"],
                    "state_id": state["state_id"],
                    "status": "missing",
                }
            )
            return refuse("missing_input", "An input object is missing", checked)
        objects.append((item["role"], state["state_id"], obj))
        checked.append(
            {"role": item["role"], "state_id": state["state_id"], "status": "available"}
        )
    failed = check_preconditions(contract, [obj for _r, _s, obj in objects])
    if failed is not None:
        for entry in checked:
            entry["status"] = "unsupported"
        return refuse("unsupported_context", f"Precondition failed: {failed}", checked)
    for entry, (_role, state_id, obj) in zip(checked, objects):
        recorded = ledger.states[state_id]["fingerprint"]
        facts = observe_object(obj)
        if recorded is None or facts.get("fingerprint") is None:
            entry["status"] = "unsupported"
            return refuse(
                "unsupported_context", "Input state has no fingerprint", checked
            )
        if status.get(state_id) == "altered":
            entry["status"] = "altered"
            return refuse(
                "input_changed", "Input data were altered in the file", checked
            )
        if facts["fingerprint"] != recorded:
            entry["status"] = "changed"
            return refuse("input_changed", "Input changed since the activity", checked)
    live = [obj for _r, _s, obj in objects]
    call_inputs = live
    if apply_context is not None:
        try:
            call_inputs = apply_context(contract, live, activity.get("context") or {})
        except IneligibleError as exc:
            for entry in checked:
                entry["status"] = "unsupported"
            return refuse(exc.code, str(exc), checked)
    try:
        parameters = decode_parameters(contract, call["parameters"])
    except IneligibleError as exc:
        return refuse(exc.code, str(exc), checked)
    return Plan(activity, contract, parameters, tuple(objects), tuple(call_inputs))
