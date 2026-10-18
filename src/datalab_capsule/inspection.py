# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Read-only inspection of the provenance of a result, shared by the editions.

An edition shows, for a selected signal or image, the activity that produced its
recorded state, the parameters and ordered inputs of that activity, the
ancestors of each input (expanded on demand), what is still available, and the
analyses recorded on the object. This module builds that description from the
ledger as a bounded JSON projection; nothing here imports an application, Qt,
React or Sigima.

Three operations are kept apart:

- *consulting* (``check_data=False``) reads the ledger and the presence of
  objects only: no fingerprint, no array copy, no computation;
- *checking the data* (``check_data=True``) compares the anchored object and the
  direct inputs and outputs of the described activity with their recorded
  states (fingerprint, ROI, metadata read by the computation, analysis
  results), with the same definition of "same state" as replay preparation
  (:func:`datalab_capsule.replay.compare_state`); it never prepares nor runs a
  computation;
- *verifying the computation* is :meth:`datalab_capsule.recorder.Recorder.verify`.

None of them changes the ledger, the objects or their metadata.

Anchoring: an object is described from its latest recorded state. The content of
the object may have changed since; until the data are checked, the description
says so (``content: "unchecked"``) and never attributes the recorded production
to the current content. An absent object is reported as such without checking
the data (nothing has to be read to know it). A state without producer is an
observed state: it has no recorded production. A ``legacy_metadata`` record is
shown with its limits; its ``observed_state_id`` does not prove that the old
computation produced that content.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from datalab_capsule.calls import is_opaque
from datalab_capsule.integrity import ProvenanceError, value_digest
from datalab_capsule.ledger import Ledger, artifact_status
from datalab_capsule.replay import compare_state

__all__ = [
    "IDENTITY_LIMIT",
    "INSPECTION_VERSION",
    "MAX_ANALYSES",
    "MAX_DEPTH",
    "MAX_INPUTS",
    "MAX_OBJECT_STATES",
    "Inspector",
    "holder_of_artifact",
    "holder_of_state",
    "metadata_digest",
    "output_state_status",
]

_logger = logging.getLogger(__name__)

#: Version of the projection returned by :class:`Inspector`.
INSPECTION_VERSION = 1
#: Length of the trail of expanded states beyond which inputs are not expandable.
MAX_DEPTH = 64
#: Inputs listed per activity (the count is always given).
MAX_INPUTS = 200
#: Analyses listed per object (the latest ones).
MAX_ANALYSES = 50
#: Recorded states listed per object (the latest ones).
MAX_OBJECT_STATES = 20
#: Limit of an observed state recorded when an appended object holds an
#: identifier already known to the ledger: what was recorded before for this
#: identifier concerns another object and is never attributed to it.
IDENTITY_LIMIT = "appended_without_provenance"


def metadata_digest(obj: Any, key: str) -> str:
    """Return the digest of the metadata value *key* of *obj* (None if absent)."""
    return value_digest(obj.metadata.get(key))


def output_state_status(
    state: Mapping[str, Any],
    obj: Any,
    facts: Any,
    file_status: str | None,
) -> str:
    """Return the status of a recorded output state against its object.

    The comparison is :func:`~datalab_capsule.replay.compare_state`, as for
    inputs: same data fingerprint and same ROI. A result whose ROI changed is
    no longer the recorded result.

    Args:
        state: Recorded output state.
        obj: Live object holding the output, or None.
        facts: ``object -> state facts``; may raise
         :class:`~datalab_capsule.recorder.StateUnavailableError`.
        file_status: Status of the state derived when the file was opened.

    Returns:
        ``missing`` (object absent, or the state was not saved in the file),
        ``altered`` (data or ROI differ from the recorded state, or cannot be
        compared with it) or ``available``.
    """
    if obj is None or file_status == "unavailable":
        return "missing"
    status, _reason = compare_state(state, facts(obj), file_status)
    return "available" if status == "available" else "altered"


class _Index:
    """Lookups derived from an append-only ledger."""

    def __init__(self, ledger: Ledger) -> None:
        self.key = (id(ledger), len(ledger.states), len(ledger.activities))
        states = ledger.states
        by_object: dict[str, list[str]] = {}
        for state_id in sorted(states, key=lambda k: states[k].get("sequence", 0)):
            by_object.setdefault(states[state_id]["object_uuid"], []).append(state_id)
        self.object_states = by_object
        #: Object UUID -> sequence of its latest identity break (see
        #: :data:`IDENTITY_LIMIT`): earlier states belong to another object.
        self.breaks: dict[str, int] = {}
        for object_uuid, state_ids in by_object.items():
            for state_id in state_ids:
                if IDENTITY_LIMIT in (states[state_id].get("limits") or []):
                    self.breaks[object_uuid] = states[state_id].get("sequence", 0)
        self.analyses: dict[str, list[str]] = {}
        self.legacy: dict[str, list[str]] = {}
        self.commands: dict[str, list[str]] = {}
        for activity in ledger.activities:
            act_id = activity["activity_id"]
            if activity.get("command_id") is not None:
                self.commands.setdefault(activity["command_id"], []).append(act_id)
            if activity["origin"] == "legacy_metadata":
                holder = activity["legacy"].get("object_uuid")
                if holder is not None:
                    self.legacy.setdefault(holder, []).append(act_id)
                continue
            outputs = activity["outputs"]
            if any("state_id" in output for output in outputs):
                continue
            # An activity without object output is an analysis: its result is
            # an artifact of the analysed object, or nothing was found.
            holders = [
                output["artifact"]["object_uuid"]
                for output in outputs
                if "artifact" in output
            ]
            holders += [
                states[item["binding"]["state_id"]]["object_uuid"]
                for item in activity["call"]["inputs"]
                if "state_id" in item["binding"]
            ]
            for holder in dict.fromkeys(holders):
                self.analyses.setdefault(holder, []).append(act_id)

    def is_before_break(self, object_uuid: str, sequence: int) -> bool:
        """Return True if a state of *object_uuid* recorded at *sequence*
        precedes an identity break (its object is not the live one)."""
        identity_break = self.breaks.get(object_uuid)
        return identity_break is not None and sequence < identity_break


def holder_of_state(index: _Index, find_object: Any, state: Mapping[str, Any]) -> Any:
    """Return the live object of a recorded state, or None.

    The single rule used to consult, check and verify: an object that took
    over the identifier of the state after an identity break (see
    :data:`IDENTITY_LIMIT`) is not the object of that state.

    Args:
        index: Lookups of the ledger (see :class:`Inspector`).
        find_object: ``object_uuid -> live object or None`` of the host.
        state: Recorded state.
    """
    if index.is_before_break(state["object_uuid"], state.get("sequence", 0)):
        return None
    return find_object(state["object_uuid"])


def holder_of_artifact(
    index: _Index,
    find_object: Any,
    ledger: Ledger,
    activity: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> Any:
    """Return the live object holding an analysis result, or None.

    An analysis stores its result in the object it analysed: when that input
    state precedes an identity break, the live object holding the identifier
    is another object, which does not hold this result.
    """
    object_uuid = artifact["object_uuid"]
    for item in activity["call"]["inputs"]:
        state_id = item["binding"].get("state_id")
        if state_id is None:
            continue
        state = ledger.states[state_id]
        if state["object_uuid"] == object_uuid:
            return holder_of_state(index, find_object, state)
    return find_object(object_uuid)


class Inspector:
    """Builds the provenance description of states, activities and objects.

    Args:
        ledger: Workspace ledger (never modified).
        host: Accesses of the edition (:class:`~datalab_capsule.recorder.RecorderHost`).
        computation: Functions of the computation library
         (:class:`~datalab_capsule.recorder.Computation`); only contract lookup
         and qualification status are used.
        state_status: Statuses derived when the workspace file was opened.
        unavailable_error: Exception raised by the host when the data of an
         object are not available locally.
    """

    def __init__(
        self,
        ledger: Ledger,
        host: Any,
        computation: Any,
        state_status: Mapping[str, str],
        unavailable_error: type[BaseException],
        index: _Index | None = None,
    ) -> None:
        self.ledger = ledger
        self.host = host
        self.computation = computation
        self.state_status = state_status
        self.unavailable_error = unavailable_error
        self.index = index if index is not None else self.build_index(ledger)
        self._facts: dict[str, Any] = {}

    @staticmethod
    def build_index(ledger: Ledger) -> _Index:
        """Return the lookups of *ledger*."""
        return _Index(ledger)

    @staticmethod
    def index_is_current(index: _Index | None, ledger: Ledger) -> bool:
        """Return True if *index* still describes *ledger* (append-only)."""
        return index is not None and index.key == (
            id(ledger),
            len(ledger.states),
            len(ledger.activities),
        )

    # -- Data checks ------------------------------------------------------

    def _holder(self, state: Mapping[str, Any]) -> Any:
        """Return the live object of a recorded state, or None.

        An object that took over the identifier of the state after an identity
        break (see :data:`IDENTITY_LIMIT`) is not the object of that state.
        """
        return holder_of_state(self.index, self.host.find_object, state)

    def _live_facts(self, obj: Any) -> Any:
        """Return the state facts of *obj*, once per description."""
        key = id(obj)
        if key not in self._facts:
            try:
                self._facts[key] = self.host.state_facts(obj)
            except self.unavailable_error:
                self._facts[key] = None
        return self._facts[key]

    def _state_check(self, state: Mapping[str, Any], obj: Any) -> str:
        """Return the status of a live object against a recorded state."""
        if obj is None:
            return "missing"
        facts = self._live_facts(obj)
        if facts is None:
            return "unavailable"
        return compare_state(state, facts, self.state_status.get(state["state_id"]))[0]

    def _output_check(self, state: Mapping[str, Any], obj: Any) -> str:
        def facts(live: Any) -> Any:
            found = self._live_facts(live)
            if found is None:
                raise self.unavailable_error("unavailable")
            return found

        try:
            return output_state_status(
                state, obj, facts, self.state_status.get(state["state_id"])
            )
        except self.unavailable_error:
            return "unavailable"

    # -- Descriptions -----------------------------------------------------

    def _support(self, activity: Mapping[str, Any]) -> dict[str, Any]:
        """Return what the local library knows of the operation of *activity*."""
        if activity["origin"] == "legacy_metadata":
            return {
                "function": "legacy",
                "verification": "unavailable",
                "reason": "The record was rebuilt from the metadata of an old "
                "workspace: its inputs, parameters and environment are unknown",
            }
        operation = activity["call"]["operation"]
        if operation is None:
            return {
                "function": "opaque",
                "verification": "unavailable",
                "reason": "The call names no operation (opaque)",
            }
        try:
            contract = self.computation.get_contract(
                operation["id"], operation["contract_version"]
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            code = self.computation.eligibility_code(exc)
            if code is None:
                _logger.warning("Contract lookup failed: %s", exc, exc_info=True)
            function = "incompatible" if code == "unsupported_contract" else "unknown"
            return {
                "function": function,
                "verification": "unavailable",
                "reason": str(exc),
            }
        status = self.computation.qualification_status(contract)
        reason = {
            "not_qualified": "Its verification is not qualified yet",
            "qualification_outdated": "Its qualification names another version",
        }.get(status)
        return {"function": "known", "verification": status, "reason": reason}

    def _input(
        self,
        position: int,
        item: Mapping[str, Any],
        trail: Sequence[str],
        check_data: bool,
    ) -> dict[str, Any]:
        binding = item["binding"]
        state_id = binding.get("state_id")
        if state_id is None:
            uuids = binding.get("object_uuids") or [None]
            uid = uuids[0]
            return {
                "position": position,
                "role": item["role"],
                "state_id": None,
                "object_uuid": uid,
                # Bound before any identity break: after one, the object holding
                # this identifier is another object
                "object_present": uid is not None
                and uid not in self.index.breaks
                and self.host.find_object(uid) is not None,
                "produced_by": None,
                "has_ancestors": False,
                "cycle": False,
                "data": "unknown",
            }
        state = self.ledger.states[state_id]
        obj = self._holder(state)
        cycle = state_id in trail
        producer = state["produced_by"]
        return {
            "position": position,
            "role": item["role"],
            "state_id": state_id,
            "object_uuid": state["object_uuid"],
            "object_present": obj is not None,
            "produced_by": producer,
            "has_ancestors": producer is not None
            and not cycle
            and len(trail) < MAX_DEPTH,
            "cycle": cycle,
            "data": self._state_check(state, obj)
            if check_data or obj is None
            else "unchecked",
        }

    def _output(
        self,
        activity: Mapping[str, Any],
        output: Mapping[str, Any],
        check_data: bool,
    ) -> dict[str, Any]:
        if "state_id" in output:
            state = self.ledger.states[output["state_id"]]
            obj = self._holder(state)
            return {
                "role": output["role"],
                "recorded": "state",
                "state_id": state["state_id"],
                "object_uuid": state["object_uuid"],
                "object_present": obj is not None,
                "status": self._output_check(state, obj)
                if check_data or obj is None
                else "unchecked",
            }
        artifact = output["artifact"]
        status = "unchecked"
        obj = holder_of_artifact(
            self.index, self.host.find_object, self.ledger, activity, artifact
        )
        if check_data or obj is None:
            value = None if obj is None else obj.metadata.get(artifact["key"])
            status = artifact_status(artifact, value)
        return {
            "role": output["role"],
            "recorded": "artifact",
            "kind": artifact["kind"],
            "object_uuid": artifact["object_uuid"],
            "object_present": obj is not None,
            "key": artifact["key"],
            "status": status,
        }

    def _context(self, activity: Mapping[str, Any], check_data: bool) -> dict[str, Any]:
        context = copy.deepcopy(activity.get("context") or {})
        reads = []
        inputs = activity["call"]["inputs"]
        for item in context.pop("metadata", None) or []:
            if not isinstance(item, dict):
                continue
            entry = {k: item.get(k) for k in ("role", "position", "key")}
            if check_data:
                entry["status"] = self._metadata_check(item, inputs)
            elif self._metadata_input_absent(item, inputs):
                entry["status"] = "missing"
            reads.append(entry)
        if reads:
            context["metadata"] = reads
        return context

    def _metadata_input_absent(
        self, item: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]]
    ) -> bool:
        """Return True if the input holding a metadata read no longer exists."""
        position = item.get("position")
        if not isinstance(position, int) or not 0 <= position < len(inputs):
            return False
        state_id = inputs[position]["binding"].get("state_id")
        return state_id is not None and (
            self._holder(self.ledger.states[state_id]) is None
        )

    def _metadata_check(
        self, item: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]]
    ) -> str:
        position = item.get("position")
        if not isinstance(position, int) or not 0 <= position < len(inputs):
            return "unknown"
        state_id = inputs[position]["binding"].get("state_id")
        if state_id is None:
            return "unknown"
        obj = self._holder(self.ledger.states[state_id])
        if obj is None:
            return "missing"
        same = metadata_digest(obj, item.get("key")) == item.get("digest")
        return "matches" if same else "changed"

    def describe_activity(
        self,
        activity_id: str,
        check_data: bool = False,
        trail: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Return the description of one activity.

        Args:
            activity_id: Activity to describe.
            check_data: Check its direct inputs and outputs (see module).
            trail: States already expanded above this activity, for the
             protection against cycles and excessive depth.

        Raises:
            KeyError: If the activity does not exist.
        """
        activity = self.ledger.activity(activity_id)
        call = activity["call"]
        items = call["inputs"]
        limits = list(activity.get("limits") or [])
        if len(items) > MAX_INPUTS:
            limits.append("inputs_truncated")
        inputs = [
            self._input(position, item, trail, check_data)
            for position, item in enumerate(items[:MAX_INPUTS])
        ]
        outputs = [
            self._output(activity, output, check_data) for output in activity["outputs"]
        ]
        env_id = activity.get("environment_id")
        environment = None
        if env_id is not None:
            record = self.ledger.environments.get(env_id)
            environment = {"environment_id": env_id, **copy.deepcopy(record or {})}
        legacy = activity["origin"] == "legacy_metadata"
        description = {
            "activity_id": activity_id,
            "command_id": activity.get("command_id"),
            "origin": activity["origin"],
            "edition": activity.get("edition"),
            "status": activity.get("status"),
            "started_at": activity.get("started_at"),
            "finished_at": activity.get("finished_at"),
            "operation": copy.deepcopy(call["operation"]),
            "opaque": is_opaque(call),
            "implementation": copy.deepcopy(activity.get("implementation")),
            "parameters": copy.deepcopy(call["parameters"]),
            "input_count": len(items),
            "inputs": inputs,
            "outputs": outputs,
            "no_result": not legacy and not activity["outputs"],
            "context": self._context(activity, check_data),
            "environment": environment,
            "limits": limits,
            "support": self._support(activity),
            "legacy": copy.deepcopy(activity["legacy"]) if legacy else None,
        }
        return description

    def _object(self, object_uuid: str) -> dict[str, Any]:
        return {
            "object_uuid": object_uuid,
            "present": self.host.find_object(object_uuid) is not None,
        }

    def _state_summary(self, state: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            key: copy.deepcopy(state.get(key))
            for key in (
                "state_id",
                "kind",
                "sequence",
                "produced_by",
                "dtype",
                "length",
                "shape",
                "units",
                "limits",
            )
            if key in state
        }
        summary["has_fingerprint"] = state.get("fingerprint") is not None
        summary["roi"] = copy.deepcopy(state.get("roi"))
        return summary

    def describe_state(
        self,
        state_id: str,
        check_data: bool = False,
        trail: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Return the description of one recorded state and of its production.

        Args:
            state_id: State to describe (e.g. an input to expand).
            check_data: Check the object against this state, and the direct
             inputs and outputs of its production.
            trail: States already expanded above this one (cycle and depth).

        Raises:
            KeyError: If the state does not exist.
        """
        state = self.ledger.states[state_id]
        object_uuid = state["object_uuid"]
        obj = self._holder(state)
        latest = self.ledger.latest_state(object_uuid)
        limits = []
        if len(trail) >= MAX_DEPTH:
            limits.append("depth_limit")
        content = (
            self._state_check(state, obj) if check_data or obj is None else "unchecked"
        )
        content = {
            "available": "matches",
            "changed": "differs",
            "altered": "differs",
            "missing": "absent",
            "unsupported": "unknown",
        }.get(content, content)
        producer = state["produced_by"]
        trail = [*trail, state_id]
        return {
            "inspection": INSPECTION_VERSION,
            "object": {"object_uuid": object_uuid, "present": obj is not None},
            "state": self._state_summary(state),
            "anchor": {
                "latest": latest is not None and latest["state_id"] == state_id,
                "content": content,
                "file_status": self.state_status.get(state_id),
            },
            "production": None
            if producer is None
            else self.describe_activity(producer, check_data, trail),
            "limits": limits,
        }

    def describe_object(
        self, object_uuid: str, check_data: bool = False
    ) -> dict[str, Any]:
        """Return the description of an object, anchored on its latest state.

        Besides :meth:`describe_state`, it lists the latest recorded states of
        the object (newest first), its legacy records and the analyses recorded
        on it (in recording order).
        """
        latest = self.ledger.latest_state(object_uuid)
        if latest is None:
            description = {
                "inspection": INSPECTION_VERSION,
                "object": self._object(object_uuid),
                "state": None,
                "anchor": {
                    "latest": False,
                    "content": "unrecorded",
                    "file_status": None,
                },
                "production": None,
                "limits": [],
            }
        else:
            description = self.describe_state(latest["state_id"], check_data)
        states = self.index.object_states.get(object_uuid, [])
        identity_break = self.index.breaks.get(object_uuid)
        if identity_break is not None:
            # What was recorded before concerns another object.
            states = [
                s
                for s in states
                if self.ledger.states[s].get("sequence", 0) >= identity_break
            ]
            description["limits"].append(IDENTITY_LIMIT)
        kept = set(states)
        description["object_states"] = [
            {
                "state_id": state_id,
                "sequence": self.ledger.states[state_id].get("sequence"),
                "produced_by": self.ledger.states[state_id]["produced_by"],
            }
            for state_id in reversed(states[-MAX_OBJECT_STATES:])
        ]
        if len(states) > MAX_OBJECT_STATES:
            description["limits"].append("object_states_truncated")
        legacy = self.index.legacy.get(object_uuid, [])
        analyses = self.index.analyses.get(object_uuid, [])
        if identity_break is not None:
            legacy = [
                act_id
                for act_id in legacy
                if self.ledger.activity(act_id)["legacy"].get("observed_state_id")
                in kept
            ]
            analyses = [
                act_id
                for act_id in analyses
                if any(
                    item["binding"].get("state_id") in kept
                    for item in self.ledger.activity(act_id)["call"]["inputs"]
                )
            ]
        description["legacy"] = [self.describe_activity(act_id) for act_id in legacy]
        if len(analyses) > MAX_ANALYSES:
            description["limits"].append("analyses_truncated")
        description["analyses"] = [
            self._analysis(act_id, check_data) for act_id in analyses[-MAX_ANALYSES:]
        ]
        return description

    def _analysis(self, activity_id: str, check_data: bool) -> dict[str, Any]:
        description = self.describe_activity(activity_id, check_data)
        analysed = [i["state_id"] for i in description["inputs"] if i["state_id"]]
        description["analysed_state_id"] = analysed[0] if analysed else None
        return description

    def command_activity_ids(self, command_id: str) -> list[str]:
        """Return the activities recorded for one command, in recording order."""
        return list(self.index.commands.get(command_id, []))

    def producing_call(
        self,
        object_uuid: str,
        resolve_function: Any,
        ineligible_error: type[ProvenanceError],
    ) -> dict[str, Any] | None:
        """Return the call that produced the latest recorded state of an object.

        This is what Processing restores. Only an activity that names an
        operation is returned: an observed state, an opaque or a legacy record
        give None (the edition then uses its own fallback). Every input is
        returned, in order, repetitions included; inputs are never replaced by
        the current selection.

        Args:
            object_uuid: Output object.
            resolve_function: ``(operation_id, version) -> contract``; raises
             *ineligible_error* when the operation is not known locally.
            ineligible_error: Error type carrying an eligibility ``code``.

        Returns:
            ``{"activity_id", "state_id", "operation", "contract",
            "eligibility", "reason", "parameters", "inputs", "context",
            "output_status"}`` or None. ``contract`` is None when the operation is
            not known locally (``eligibility`` and ``reason`` then say why);
            ``inputs`` items are ``{"role", "state_id", "object_uuid",
            "present"}``; ``output_status`` is the status of the output state
            read in the workspace file, if any.
        """
        state = self.ledger.latest_state(object_uuid)
        if state is None or state["produced_by"] is None:
            return None
        activity = self.ledger.activity(state["produced_by"])
        call = activity["call"]
        if activity["origin"] == "legacy_metadata" or is_opaque(call):
            return None
        operation = call["operation"]
        contract = eligibility = reason = None
        try:
            contract = resolve_function(operation["id"], operation["contract_version"])
        except ineligible_error as exc:
            eligibility, reason = getattr(exc, "code", None), str(exc)
        inputs = []
        for item in call["inputs"]:
            input_state = self.ledger.states[item["binding"]["state_id"]]
            inputs.append(
                {
                    "role": item["role"],
                    "state_id": input_state["state_id"],
                    "object_uuid": input_state["object_uuid"],
                    "present": self._holder(input_state) is not None,
                }
            )
        return {
            "activity_id": activity["activity_id"],
            "state_id": state["state_id"],
            "operation": copy.deepcopy(operation),
            "contract": contract,
            "eligibility": eligibility,
            "reason": reason,
            "parameters": copy.deepcopy(call["parameters"]),
            "inputs": inputs,
            "context": copy.deepcopy(activity.get("context") or {}),
            "output_status": self.state_status.get(state["state_id"]),
        }
