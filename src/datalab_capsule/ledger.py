# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Workspace provenance ledger, version 1.

The ledger holds facts about a workspace: execution environments, states (one
version of the scientific content of an object) and activities (one execution of
one function call). Arrays are never copied into it. Activities are append-only.

State ordering: each state carries an integer ``sequence``. The latest state of an
object is the one with the highest sequence; it is reused when the object is
observed again with the same fingerprint.
"""

from __future__ import annotations

import copy
import datetime
import json
import uuid
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from datalab_capsule._schema import load_schema, schema_errors
from datalab_capsule.calls import CallError, is_opaque, validate_call
from datalab_capsule.environment import EDITIONS, environment_id
from datalab_capsule.integrity import ProvenanceError, load_json, value_digest

__all__ = [
    "LEDGER_SCHEMA",
    "LEDGER_SCHEMA_VERSION",
    "ORIGINS",
    "Ledger",
    "LedgerError",
    "artifact_status",
    "load_schema",
    "utc_timestamp",
    "validate_ledger",
]

LEDGER_SCHEMA = "datalab-provenance-ledger"
LEDGER_SCHEMA_VERSION = 1
#: Origins written by :meth:`Ledger.record_activity`; ``legacy_metadata`` is
#: written by :meth:`Ledger.record_legacy_activity`; ``recipe`` and ``opaque``
#: are reserved and accepted when reading.
ORIGINS = ("ordinary", "recompute_in_place", "history_replay")


class LedgerError(ProvenanceError, ValueError):
    """Raised when a ledger is invalid or an operation would break it."""


def artifact_status(artifact: Mapping[str, Any], value: Any) -> str:
    """Return the status of a recorded analysis result against its current value.

    Args:
        artifact: Recorded artifact (``kind``, ``object_uuid``, ``key``,
         ``digest``).
        value: Current value stored under the artifact's key, or None.

    Returns:
        ``available`` when the value is the recorded result, ``replaced`` when
        another result now holds the key, ``missing`` when the key is absent.
    """
    if value is None:
        return "missing"
    return "available" if value_digest(value) == artifact.get("digest") else "replaced"


def utc_timestamp() -> str:
    """Return the current UTC time as an ISO 8601 string (milliseconds, ``Z``)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state_order(states: Mapping[str, Any]) -> list[str]:
    """Return state identifiers ordered by sequence, then insertion order."""
    keys = list(states)
    return sorted(
        keys, key=lambda k: (states[k].get("sequence", keys.index(k)), keys.index(k))
    )


def validate_ledger(data: Any) -> None:
    """Validate a ledger against its JSON Schema and semantic rules.

    Raises:
        LedgerError: With the first problems found.
    """
    errors = schema_errors("ledger-1", data)
    if errors:
        raise LedgerError("Invalid ledger: " + "; ".join(errors[:5]))
    problems: list[str] = []
    environments = data["environments"]
    states = data["states"]
    for env_id, record in environments.items():
        if environment_id(record) != env_id:
            problems.append(f"environment {env_id}: identifier does not match")
    sequences = [s["sequence"] for s in states.values() if "sequence" in s]
    if len(sequences) != len(set(sequences)):
        problems.append("state sequences must be unique")
    activity_ids: set[str] = set()
    produced: dict[str, str] = {}
    for index, activity in enumerate(data["activities"]):
        act_id = activity["activity_id"]
        where = f"activities[{index}] ({act_id})"
        if act_id in activity_ids:
            problems.append(f"{where}: duplicate activity_id")
        activity_ids.add(act_id)
        legacy = activity["origin"] == "legacy_metadata"
        try:
            validate_call(activity["call"], allow_object_bindings=legacy)
        except CallError as exc:
            problems.append(f"{where}: {exc}")
            continue
        for item in activity["call"]["inputs"]:
            state_id = item["binding"].get("state_id")
            if state_id is not None and state_id not in states:
                problems.append(f"{where}: unknown input state")
        if legacy and not is_opaque(activity["call"]):
            problems.append(f"{where}: a legacy activity never names an operation")
        if legacy:
            observed = activity["legacy"].get("observed_state_id")
            if observed is not None and observed not in states:
                problems.append(f"{where}: unknown observed state")
        if (
            is_opaque(activity["call"])
            and activity["implementation"] is None
            and not legacy
        ):
            problems.append(f"{where}: an opaque call needs an implementation")
        env_id = activity["environment_id"]
        if env_id is not None and env_id not in environments:
            problems.append(f"{where}: unknown environment")
        for output in activity["outputs"]:
            state_id = output.get("state_id")
            if state_id is None:
                continue
            if state_id not in states:
                problems.append(f"{where}: unknown output state {state_id}")
            elif states[state_id]["produced_by"] != act_id:
                problems.append(
                    f"{where}: output state {state_id} names another producer"
                )
            elif state_id in produced:
                problems.append(f"{where}: output state {state_id} produced twice")
            produced[state_id] = act_id
    for state_id, state in states.items():
        if state["state_id"] != state_id:
            problems.append(f"state {state_id}: key does not match state_id")
        producer = state["produced_by"]
        if producer is not None and produced.get(state_id) != producer:
            problems.append(f"state {state_id}: producer does not list it as output")
    if problems:
        raise LedgerError("Invalid ledger: " + "; ".join(problems[:5]))


class Ledger:
    """Append-only provenance ledger of one workspace.

    Args:
        workspace_id: Workspace identifier; a new UUID4 when omitted.
    """

    def __init__(self, workspace_id: str | None = None) -> None:
        self._data: dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "workspace_id": workspace_id or str(uuid.uuid4()),
            "environments": {},
            "states": {},
            "activities": [],
        }
        self._latest: dict[str, str] = {}
        self._activity_index: dict[str, int] = {}
        self._next_sequence = 0

    # -- Serialisation -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: Any) -> Ledger:
        """Build a ledger from its JSON-compatible form, after validation.

        Raises:
            LedgerError: If *data* is not a valid ledger.
        """
        validate_ledger(data)
        ledger = cls(data["workspace_id"])
        ledger._data = copy.deepcopy(data)
        states = ledger._data["states"]
        for state_id in _state_order(states):
            ledger._latest[states[state_id]["object_uuid"]] = state_id
        ledger._next_sequence = 1 + max(
            (s.get("sequence", i) for i, s in enumerate(states.values())), default=-1
        )
        ledger._activity_index = {
            a["activity_id"]: i for i, a in enumerate(ledger._data["activities"])
        }
        return ledger

    @classmethod
    def from_json(cls, text: str) -> Ledger:
        """Build a ledger from JSON text.

        Raises:
            LedgerError: If *text* is not valid JSON or not a valid ledger.
        """
        try:
            data = load_json(text)
        except (TypeError, ValueError) as exc:
            raise LedgerError(f"Ledger is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Return a deep copy of the JSON-compatible ledger."""
        return copy.deepcopy(self._data)

    def to_json(self) -> str:
        """Return the ledger as compact JSON text (UTF-8 friendly)."""
        return json.dumps(
            self._data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

    def validate(self) -> None:
        """Validate the ledger (schema and semantic rules)."""
        validate_ledger(self._data)

    # -- Read access -------------------------------------------------------

    @property
    def workspace_id(self) -> str:
        """Workspace identifier."""
        return self._data["workspace_id"]

    @property
    def environments(self) -> Mapping[str, dict[str, Any]]:
        """Environment records by identifier (read-only)."""
        return MappingProxyType(self._data["environments"])

    @property
    def states(self) -> Mapping[str, dict[str, Any]]:
        """States by identifier (read-only)."""
        return MappingProxyType(self._data["states"])

    @property
    def activities(self) -> tuple[dict[str, Any], ...]:
        """Activities in append order."""
        return tuple(self._data["activities"])

    def activity(self, activity_id: str) -> dict[str, Any]:
        """Return an activity.

        Raises:
            KeyError: If the activity does not exist.
        """
        return self._data["activities"][self._activity_index[activity_id]]

    def latest_state(self, object_uuid: str) -> dict[str, Any] | None:
        """Return the latest state of an object, or None."""
        state_id = self._latest.get(object_uuid)
        return None if state_id is None else self._data["states"][state_id]

    def object_uuids(self) -> list[str]:
        """Return the identifiers of every object that has a state."""
        return list(self._latest)

    # -- Mutation ----------------------------------------------------------

    def add_environment(self, record: dict[str, Any]) -> str:
        """Add an environment record (deduplicated) and return its identifier."""
        env_id = environment_id(record)
        self._data["environments"].setdefault(env_id, copy.deepcopy(record))
        return env_id

    def _new_state(
        self, object_uuid: str, facts: dict[str, Any], produced_by: str | None
    ) -> str:
        state_id = str(uuid.uuid4())
        state = {"state_id": state_id, "object_uuid": object_uuid}
        state.update(copy.deepcopy(facts))
        state["produced_by"] = produced_by
        state["sequence"] = self._next_sequence
        self._next_sequence += 1
        self._data["states"][state_id] = state
        self._latest[object_uuid] = state_id
        return state_id

    def observe(
        self, object_uuid: str, facts: dict[str, Any], fresh: bool = False
    ) -> str:
        """Return the state of an object, reusing its latest state if unchanged.

        Args:
            object_uuid: Persistent object identifier.
            facts: State facts, e.g. from
             :func:`datalab_capsule.integrity.signal_state_facts`.
            fresh: Always create a new state (e.g. when another object now
             holds this identifier), even if the latest one has the same facts.

        Returns:
            State identifier.
        """
        latest = self.latest_state(object_uuid)
        if (
            not fresh
            and latest is not None
            and facts.get("fingerprint") is not None
            and latest["fingerprint"] == facts["fingerprint"]
            and latest["kind"] == facts["kind"]
            and latest.get("roi") == facts.get("roi")
        ):
            return latest["state_id"]
        return self._new_state(object_uuid, facts, None)

    def record_activity(
        self,
        *,
        call: dict[str, Any],
        outputs: Iterable[tuple[str, str, dict[str, Any]]],
        environment: dict[str, Any],
        edition: str,
        origin: str,
        implementation: dict[str, Any] | None = None,
        command_id: str | None = None,
        context: dict[str, Any] | None = None,
        limits: Iterable[str] = (),
        started_at: str | None = None,
        finished_at: str | None = None,
        artifacts: Iterable[tuple[str, str, str, str, Any]] = (),
    ) -> dict[str, Any]:
        """Append one completed activity and create its output states.

        Args:
            call: Operation call whose inputs are bound to existing states.
            outputs: Ordered ``(role, object_uuid, facts)`` triples.
            environment: Environment record of the execution.
            edition: ``"desktop"`` or ``"web"``.
            origin: Activity origin (see :data:`ORIGINS`).
            implementation: Informative implementation descriptor.
            command_id: Identifier shared by the executions of one user command.
            context: Execution context (ROI, mask, X alignment...).
            limits: Reasons why the activity is not replayable.
            started_at: ISO 8601 start time (informative).
            finished_at: ISO 8601 end time (informative).
            artifacts: Ordered ``(role, kind, object_uuid, key, value)`` analysis
             results stored in an object's metadata, recorded after the state
             outputs with the digest of *value* (see :func:`artifact_status`).

        Returns:
            The appended activity.

        Raises:
            LedgerError: If the activity is inconsistent.
        """
        try:
            validate_call(call)
        except CallError as exc:
            raise LedgerError(str(exc)) from exc
        if edition not in EDITIONS:
            raise LedgerError(f"Unknown edition: {edition!r}")
        if origin not in ORIGINS:
            raise LedgerError(f"Unknown origin: {origin!r}")
        if is_opaque(call) and implementation is None:
            raise LedgerError("An opaque call needs an implementation")
        for item in call["inputs"]:
            if item["binding"]["state_id"] not in self._data["states"]:
                raise LedgerError("Input states must be observed before recording")
        activity_id = str(uuid.uuid4())
        env_id = self.add_environment(environment)
        output_items = [
            {"role": role, "state_id": self._new_state(obj_uuid, facts, activity_id)}
            for role, obj_uuid, facts in outputs
        ]
        output_items.extend(
            {
                "role": role,
                "artifact": {
                    "kind": kind,
                    "object_uuid": obj_uuid,
                    "key": key,
                    "digest": value_digest(value),
                },
            }
            for role, kind, obj_uuid, key, value in artifacts
        )
        now = utc_timestamp()
        activity = {
            "activity_id": activity_id,
            "command_id": command_id,
            "call": copy.deepcopy(call),
            "implementation": copy.deepcopy(implementation),
            "outputs": output_items,
            "context": copy.deepcopy(context)
            if context is not None
            else {"roi": None, "mask": None, "x_alignment": None},
            "limits": list(limits),
            "environment_id": env_id,
            "edition": edition,
            "origin": origin,
            "status": "completed",
            "started_at": started_at or now,
            "finished_at": finished_at or now,
        }
        self._activity_index[activity_id] = len(self._data["activities"])
        self._data["activities"].append(activity)
        return activity

    def record_legacy_activity(
        self,
        *,
        inputs: Iterable[tuple[str, str]],
        legacy: dict[str, Any],
        limits: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Append an activity rebuilt from processing metadata of an old workspace.

        What the metadata does not say stays unknown: inputs are bound to objects
        (their state at execution time is unknown), the call is opaque with no
        parameters, and environment, edition, implementation and times are null.
        The historical result is not attested, so the activity has no outputs;
        the state of the holding object observed when the workspace was opened
        may be named by ``legacy["observed_state_id"]``, an observed state that
        the activity did not produce. Such an activity is never replayed.

        Args:
            inputs: Ordered ``(role, object_uuid)`` pairs.
            legacy: ``{"key", "object_uuid", "record", "saved_by_version",
             "issues"}`` and optionally ``"observed_state_id"`` (see
             :mod:`datalab_capsule.legacy`).
            limits: Reasons why the activity is not replayable.

        Returns:
            The appended activity.

        Raises:
            LedgerError: If the activity is inconsistent.
        """
        call = {
            "operation": None,
            "parameters": None,
            "inputs": [
                {"role": role, "binding": {"object_uuids": [obj_uuid]}}
                for role, obj_uuid in inputs
            ],
        }
        try:
            validate_call(call, allow_object_bindings=True)
        except CallError as exc:
            raise LedgerError(str(exc)) from exc
        observed = legacy.get("observed_state_id")
        if observed is not None and observed not in self._data["states"]:
            raise LedgerError("The observed state must exist")
        activity_id = str(uuid.uuid4())
        activity = {
            "activity_id": activity_id,
            "command_id": None,
            "call": call,
            "implementation": None,
            "outputs": [],
            "context": {},
            "limits": list(limits),
            "environment_id": None,
            "edition": None,
            "origin": "legacy_metadata",
            "status": "completed",
            "started_at": None,
            "finished_at": None,
            "legacy": copy.deepcopy(legacy),
        }
        self._activity_index[activity_id] = len(self._data["activities"])
        self._data["activities"].append(activity)
        return activity
