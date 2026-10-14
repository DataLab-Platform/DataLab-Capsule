# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Provenance recorder shared by the DataLab editions.

The recorder holds the workspace ledger of one application and implements what
both editions need: building operation calls from the description of the
computation library, recording executions (``begin``, ``complete``,
``complete_analysis``, deferred finalization), artifact statuses, replay
preparation and verification, and the ledger lifecycle (reset, save, load,
import of old workspaces).

An edition only provides its accesses (:class:`RecorderHost`): object UUIDs,
lookup and iteration, the kind of an object, its state facts, and whether an
operation named in old metadata is known. The computation library is injected
as plain functions (:class:`Computation`, see :func:`computation_from_sigima`);
nothing here imports an application, Sigima or Qt. The correspondence between
the errors of the computation library and the eligibility codes of reports is
defined here, once.

Capture never breaks processing: if recording fails, no activity is recorded and
the failure is logged and counted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import Callable, Generator, Iterable, Sequence
from typing import Any

from datalab_capsule.calls import make_call
from datalab_capsule.compare import (
    build_report,
    compare_environments,
    compare_rows,
)
from datalab_capsule.environment import collect_environment, environment_id
from datalab_capsule.hdf5 import load_ledger, save_ledger
from datalab_capsule.integrity import ProvenanceError, signal_rows, state_facts
from datalab_capsule.ledger import Ledger, artifact_status, utc_timestamp
from datalab_capsule.legacy import (
    PROCESSING_KEY,
    LegacyObject,
    import_legacy,
    legacy_summary,
)
from datalab_capsule.replay import IneligibleError, Plan, Refusal, prepare_activity

__all__ = [
    "Computation",
    "PendingActivity",
    "Recorder",
    "RecorderHost",
    "StateUnavailableError",
    "comparison_rows",
    "computation_from_sigima",
]

_logger = logging.getLogger(__name__)


class StateUnavailableError(ProvenanceError):
    """Raised by a host when the data of an object are not available to observe.

    For example, DataLab-Web raises it for objects whose arrays were spilled to
    disk to save memory.
    """


@dataclasses.dataclass
class RecorderHost:
    """Accesses provided by an edition.

    Attributes:
        edition: ``"desktop"`` or ``"web"``.
        edition_version: Version of the application, recorded in environments.
        object_uuid: Persistent UUID of an object, or None.
        find_object: Object lookup by UUID (None when absent).
        iter_objects: Signals and images of the workspace, in panel order.
        object_kind: ``"signal"``, ``"image"`` or None for anything else.
        is_known_operation: ``(kind, name, plugin origin) -> bool``: whether an
         operation named in old metadata is known to the edition.
        state_facts: State facts of an object; may raise
         :class:`StateUnavailableError`.
    """

    edition: str
    edition_version: Callable[[], str]
    object_uuid: Callable[[Any], str | None]
    find_object: Callable[[str], Any]
    iter_objects: Callable[[], Iterable[Any]]
    object_kind: Callable[[Any], str | None]
    is_known_operation: Callable[[str, str, Any], bool]
    state_facts: Callable[[Any], dict[str, Any]] = state_facts


@dataclasses.dataclass(frozen=True)
class Computation:
    """Functions of the computation library used by the recorder.

    Attributes:
        describe_call: ``(func, inputs, param, *, roles=None, contract=...) ->
         description`` (see ``sigima.proc.execution.describe_call``).
        get_contract: ``(operation_id, contract_version) -> contract``; raises
         when the operation or the version is unknown.
        prepare_execution: ``(contract, inputs, *, values, context) -> prepared``
         (see ``sigima.proc.execution.prepare_execution``).
        normalize_outputs: ``(contract, result) -> [(role, value)]``.
        eligibility_code: Eligibility code of an error raised by the functions
         above, or None for an unexpected error.
        comparison_rule: ``(contract) -> rule``: comparison rule declared by the
         qualification, as plain data, or None.
        check_preconditions: Provisional ``(contract, objects) -> failing check
         or None``.
    """

    describe_call: Callable[..., dict[str, Any]]
    get_contract: Callable[[str, int], Any]
    prepare_execution: Callable[..., Any]
    normalize_outputs: Callable[[Any, Any], list[tuple[str, Any]]]
    eligibility_code: Callable[[BaseException], str | None]
    comparison_rule: Callable[[Any], dict[str, Any] | None] = lambda _contract: None
    check_preconditions: Callable[[Any, list[Any]], str | None] = (
        lambda _contract, _objects: None
    )


def computation_from_sigima(execution: Any, contracts: Any) -> Computation:
    """Return the :class:`Computation` of Sigima, from its modules.

    The modules are passed by the application (``sigima.proc.execution`` and
    ``sigima.proc.contracts``); they are used by attribute, never imported here.
    This is the only place where Sigima errors are mapped to eligibility codes.
    """
    execution_codes = {
        "invalid_parameters": "invalid_parameters",
        "input_mismatch": "unsupported_context",
        "unsupported_context": "unsupported_context",
    }

    def eligibility_code(exc: BaseException) -> str | None:
        if isinstance(exc, contracts.UnknownOperationError):
            return "unsupported_operation"
        if isinstance(exc, contracts.IncompatibleContractError):
            return "unsupported_contract"
        if isinstance(exc, contracts.InvalidParametersError):
            return "invalid_parameters"
        if isinstance(exc, contracts.XAlignmentError):
            return "unsupported_context"
        if isinstance(exc, execution.ExecutionError):
            return execution_codes.get(exc.code)
        return None

    return Computation(
        describe_call=execution.describe_call,
        get_contract=contracts.get_operation_contract,
        prepare_execution=execution.prepare_execution,
        normalize_outputs=execution.normalize_outputs,
        eligibility_code=eligibility_code,
        comparison_rule=lambda contract: (
            None if contract.comparison is None else contract.comparison.to_dict()
        ),
        check_preconditions=lambda contract, objects: contract.check_preconditions(
            objects
        ),
    )


def comparison_rows(obj: Any, kind: str) -> dict[str, Any]:
    """Return the rows of an object compared by the comparison rules.

    Signals: the rows of :func:`~datalab_capsule.integrity.signal_rows`. Images:
    the data, then the coordinates (``x0``, ``y0``, ``dx``, ``dy`` or
    ``xcoords``, ``ycoords``).
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    if kind == "signal":
        return dict(signal_rows(obj.x, obj.y, obj.dx, obj.dy))
    rows: dict[str, Any] = {"data": np.asarray(obj.data)}
    if obj.is_uniform_coords:
        for name in ("x0", "y0", "dx", "dy"):
            rows[name] = np.asarray([getattr(obj, name)], dtype=np.float64)
    else:
        rows["xcoords"] = np.asarray(obj.xcoords, dtype=np.float64)
        rows["ycoords"] = np.asarray(obj.ycoords, dtype=np.float64)
    return rows


def _units(obj: Any) -> tuple[str, ...]:
    return tuple(getattr(obj, f"{axis}unit", "") or "" for axis in "xyz")


@dataclasses.dataclass
class PendingActivity:
    """An execution whose input states were recorded, awaiting its output.

    Attributes:
        call: Operation call bound to the input states.
        implementation: Informative implementation descriptor.
        limits: Reasons why the activity is not replayable.
        command_id: Identifier shared by the executions of one user command.
        origin: Activity origin.
        started_at: Start time.
        context: Execution context (X alignment, global options...).
    """

    call: dict[str, Any]
    implementation: dict[str, Any]
    limits: list[str]
    command_id: str | None
    origin: str
    started_at: str
    context: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def x_alignment(self) -> dict[str, Any] | None:
        """X-alignment record applied to the inputs, or None."""
        return self.context.get("x_alignment")

    @x_alignment.setter
    def x_alignment(self, record: dict[str, Any] | None) -> None:
        self.context["x_alignment"] = record


class _Deferred:
    """Pending activities held until temporary outputs get their final UUIDs."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.items: list[tuple[PendingActivity, str]] = []


class Recorder:
    """Workspace provenance ledger of an edition.

    Args:
        host: Accesses provided by the edition.
        computation: Functions of the computation library.
    """

    #: Default roles of calls without contract, by number of inputs.
    DEFAULT_ROLES = {1: ("source",), 2: ("source", "operand")}

    def __init__(self, host: RecorderHost, computation: Computation) -> None:
        self.host = host
        self.computation = computation
        self._environment: dict[str, Any] | None = None
        self._deferred: _Deferred | None = None
        self.ledger = Ledger()
        self.state_status: dict[str, str] = {}
        self.notices: list[str] = []
        self.capture_failures = 0
        #: ``"loaded"`` or ``"absent"`` after opening a workspace file, else None.
        self.file_status: str | None = None

    # -- Workspace lifecycle ------------------------------------------------

    def reset(self) -> None:
        """Start an empty ledger for a new workspace."""
        self.ledger = Ledger()
        self.file_status = None
        self.state_status = {}
        self.notices = []
        self._deferred = None

    @property
    def environment(self) -> dict[str, Any]:
        """Environment record of this session."""
        if self._environment is None:
            self._environment = collect_environment(
                self.host.edition, self.host.edition_version()
            )
        return self._environment

    def invalidate_environment(self) -> None:
        """Collect the environment again (e.g. after a version change)."""
        self._environment = None

    def save(self, h5file: Any) -> None:
        """Write the ledger block into a workspace file, after its panels."""
        save_ledger(h5file, self.ledger)

    @staticmethod
    def read(h5file: Any) -> tuple[Ledger, dict[str, str]] | None:
        """Read and validate the block of a workspace file.

        Returns:
            ``(ledger, state_status)``, or None without block.

        Raises:
            ProvenanceFormatError: If the block is invalid.
        """
        return load_ledger(h5file)

    def load(
        self,
        block: tuple[Ledger, dict[str, str]] | None,
        replaced: bool,
        saved_version: str | None = None,
        current_version: str | None = None,
    ) -> None:
        """Adopt the ledger read from a workspace file, after its objects.

        Processing records of objects unknown to the ledger (workspaces saved
        without provenance) are then imported as ``legacy_metadata`` activities.

        Args:
            block: ``(ledger, state_status)`` read before the workspace was
             replaced, or None when the file has no provenance block.
            replaced: False when the file was appended to the current workspace:
             ledgers are not merged and the file's block is ignored.
            saved_version: Application version that saved the file, as stored.
            current_version: Version compared with *saved_version*, or None.
        """
        if not replaced:
            if block is not None:
                message = (
                    "Provenance import into an existing workspace is not supported yet"
                )
                _logger.warning(message)
                self.notices.append(message)
            return
        self.reset()
        if block is None:
            self.file_status = "absent"
        else:
            self.ledger, self.state_status = block
            self.file_status = "loaded"
        self._import_legacy(saved_version, current_version)

    def _import_legacy(
        self, saved_version: str | None, current_version: str | None
    ) -> None:
        """Import the processing records of objects unknown to the ledger."""
        try:
            known = set(self.ledger.object_uuids())
            objects = []
            for obj in self.host.iter_objects():
                uid = self.host.object_uuid(obj)
                kind = self.host.object_kind(obj)
                if uid is None or kind is None:
                    continue
                facts = None
                if uid not in known and PROCESSING_KEY in obj.metadata:
                    try:
                        facts = self.facts(obj)
                    except StateUnavailableError:
                        facts = None
                objects.append(LegacyObject(uid, kind, obj.metadata, facts))
            activities = import_legacy(
                self.ledger,
                objects,
                is_known=self.host.is_known_operation,
                saved_version=saved_version,
                current_version=current_version,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return
        if activities:
            message = (
                f"{len(activities)} processing records saved without provenance "
                "were imported as legacy activities (never replayed)"
            )
            _logger.info(message)
            self.notices.append(message)

    def legacy_summary(self) -> dict[str, Any]:
        """Return the legacy activities of the ledger and the issues found."""
        return legacy_summary(self.ledger)

    # -- Capture ------------------------------------------------------------

    def _capture_failed(self, exc: Exception) -> None:
        self.capture_failures += 1
        _logger.warning("Provenance capture failed: %s", exc, exc_info=True)

    def facts(self, obj: Any) -> dict[str, Any]:
        """Return the state facts of an object.

        Raises:
            StateUnavailableError: If the object's data are not available.
        """
        return self.host.state_facts(obj)

    def observe(self, obj: Any) -> str:
        """Return the state of *obj*, reusing its latest state if unchanged."""
        return self.ledger.observe(self.host.object_uuid(obj), self.facts(obj))

    def _build_call(
        self,
        func: Callable,
        param: Any,
        state_ids: list[str],
        objs: list[Any],
        roles: Sequence[str],
    ) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        """Return the call, its limits and the implementation of an execution."""
        description = self.computation.describe_call(func, objs, param, roles=roles)
        operation = description["operation"]
        if (
            operation is not None
            and description["qualification"]["status"] == "qualified"
            and description["parameters"] is not None
            and not description["limits"]
        ):
            contract = self.computation.get_contract(
                operation["id"], operation["contract_version"]
            )
            if self.computation.check_preconditions(contract, objs) is None:
                call = make_call(
                    operation["id"],
                    operation["contract_version"],
                    description["parameters"],
                    list(zip(description["roles"], state_ids)),
                )
                return call, [], description["implementation"]
        opaque = self.computation.describe_call(
            func, objs, param, roles=roles, contract=None
        )
        call = make_call(None, None, opaque["parameters"], list(zip(roles, state_ids)))
        return call, list(opaque["limits"]), opaque["implementation"]

    def begin(
        self,
        func: Callable,
        param: Any,
        source: Any,
        command_id: str | None = None,
        origin: str = "ordinary",
        x_alignment: dict[str, Any] | None = None,
        roles: Sequence[str] | None = None,
        limits: Sequence[str] = (),
        context: dict[str, Any] | None = None,
    ) -> PendingActivity | None:
        """Record the input states of an execution, before it runs.

        Args:
            func: Computation function.
            param: Effective parameter argument, or None.
            source: Source signal or image, or the original input objects of a
             multi-input execution (before any alignment or interpolation).
            command_id: Identifier shared by the executions of one command.
            origin: Activity origin.
            x_alignment: X-alignment record applied to the inputs, or None.
            roles: Input roles of a call without contract; by default
             ``source``, or ``source`` and ``operand`` for two inputs.
            limits: Extra reasons why the activity is not replayable.
            context: Execution context of the prepared computation (X alignment,
             global options), if known before it runs.

        Returns:
            A pending activity, or None when the execution is not captured.
        """
        objs = list(source) if isinstance(source, Sequence) else [source]
        if roles is None:
            roles = self.DEFAULT_ROLES.get(len(objs))
        if (
            not objs
            or roles is None
            or len(roles) != len(objs)
            or not all(self.host.object_kind(obj) is not None for obj in objs)
        ):
            return None
        try:
            state_ids = [self.observe(obj) for obj in objs]
            call, call_limits, implementation = self._build_call(
                func, param, state_ids, objs, roles
            )
            pending = PendingActivity(
                call=call,
                implementation=implementation,
                limits=call_limits + list(limits),
                command_id=command_id,
                origin=origin,
                started_at=utc_timestamp(),
                context=dict(context or {}),
            )
            if x_alignment is not None:
                pending.x_alignment = x_alignment
            return pending
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return None

    def _record(
        self,
        pending: PendingActivity,
        outputs: list[tuple[str, str, dict[str, Any]]],
        origin: str,
        artifacts: Sequence[tuple[str, str, str, str, Any]] = (),
    ) -> dict[str, Any]:
        context = {"roi": None, "mask": None, "x_alignment": None}
        context.update(pending.context)
        return self.ledger.record_activity(
            call=pending.call,
            outputs=outputs,
            artifacts=artifacts,
            environment=self.environment,
            edition=self.host.edition,
            origin=origin,
            implementation=pending.implementation,
            command_id=pending.command_id,
            limits=pending.limits,
            context=context,
            started_at=pending.started_at,
        )

    def complete(
        self, pending: PendingActivity | None, output: Any
    ) -> dict[str, Any] | None:
        """Record a completed execution once its output was inserted.

        Outputs that are not signals or images, or were not inserted in the
        workspace (e.g. History output suppression), are not recorded.

        Returns:
            The recorded activity, or None.
        """
        if pending is None or self.host.object_kind(output) is None:
            return None
        try:
            output_uuid = self.host.object_uuid(output)
            if output_uuid is None or self.host.find_object(output_uuid) is not output:
                return None
            if self._deferred is not None:
                self._deferred.items.append((pending, output_uuid))
                return None
            return self._record(
                pending, [("result", output_uuid, self.facts(output))], pending.origin
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return None

    def complete_analysis(
        self, pending: PendingActivity | None, obj: Any, kind: str, key: str
    ) -> dict[str, Any] | None:
        """Record an analysis whose result was stored in *obj*'s metadata.

        The digest of the stored result is recorded, so that a later analysis
        writing the same key is never taken for this one (see
        :meth:`artifact_statuses`).

        Args:
            pending: Pending activity of the analysis.
            obj: Analysed object, which holds the result.
            kind: Result kind (``geometry`` or ``table``).
            key: Metadata key of the result.

        Returns:
            The recorded activity, or None.
        """
        if pending is None:
            return None
        try:
            origin = pending.origin if self._deferred is None else self._deferred.origin
            artifact = (
                "result",
                kind,
                self.host.object_uuid(obj),
                key,
                obj.metadata[key],
            )
            return self._record(pending, [], origin, artifacts=[artifact])
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return None

    def artifact_statuses(self, activity_id: str) -> list[dict[str, str]]:
        """Return the status of each analysis result recorded by an activity.

        Returns:
            One ``{"object_uuid", "key", "status"}`` per artifact output;
            ``status`` is ``available`` (the object still holds this result),
            ``replaced`` (another result now holds the key) or ``missing``.
        """
        statuses = []
        for output in self.ledger.activity(activity_id)["outputs"]:
            artifact = output.get("artifact")
            if artifact is None:
                continue
            obj = self.host.find_object(artifact["object_uuid"])
            value = None if obj is None else obj.metadata.get(artifact["key"])
            statuses.append(
                {
                    "object_uuid": artifact["object_uuid"],
                    "key": artifact["key"],
                    "status": artifact_status(artifact, value),
                }
            )
        return statuses

    @contextlib.contextmanager
    def deferred(self, origin: str) -> Generator[_Deferred, None, None]:
        """Hold completed activities until :meth:`finalize_deferred` remaps them.

        Used when temporary outputs are committed onto recorded objects after the
        computation (History replay). Pending activities left when the context
        exits are dropped.
        """
        previous = self._deferred
        self._deferred = _Deferred(origin)
        try:
            yield self._deferred
        finally:
            self._deferred = previous

    def finalize_deferred(self, deferred: _Deferred, remap: dict[str, str]) -> int:
        """Record held activities with their final output UUIDs.

        Args:
            deferred: Context returned by :meth:`deferred`.
            remap: Temporary output UUID -> final object UUID. Activities whose
             temporary output is not remapped are dropped.

        Returns:
            Number of recorded activities.
        """
        recorded = 0
        items, deferred.items = deferred.items, []
        for pending, temp_uuid in items:
            final_uuid = remap.get(temp_uuid)
            output = None if final_uuid is None else self.host.find_object(final_uuid)
            if self.host.object_kind(output) is None:
                continue
            try:
                self._record(
                    pending,
                    [("result", final_uuid, self.facts(output))],
                    deferred.origin,
                )
                recorded += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._capture_failed(exc)
        return recorded

    # -- Replay -------------------------------------------------------------

    def _ineligible(self, exc: BaseException) -> IneligibleError:
        code = self.computation.eligibility_code(exc)
        if code is None:
            raise exc
        return IneligibleError(code, str(exc))

    def resolve_contract(self, operation_id: str, version: int) -> Any:
        """Return the qualified contract of a recorded operation.

        Raises:
            IneligibleError: ``unsupported_operation`` (unknown or not
             qualified) or ``unsupported_contract``.
        """
        try:
            contract = self.computation.get_contract(operation_id, version)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise self._ineligible(exc) from exc
        if not contract.qualified:
            raise IneligibleError("unsupported_operation", "Contract not qualified")
        return contract

    def prepare(self, activity_id: str) -> Plan | Refusal:
        """Prepare a recorded activity for replay (no GUI selection needed)."""
        return self.prepare_replay(activity_id)[0]

    def prepare_replay(self, activity_id: str) -> tuple[Plan | Refusal, Any]:
        """Prepare a recorded activity for replay, with its prepared execution.

        Returns:
            ``(plan or refusal, prepared execution or None)``; the prepared
            execution applies the recorded parameters and context to the live
            inputs, which are never modified.
        """
        activity = self.ledger.activity(activity_id)
        holder: dict[str, Any] = {}

        def apply_context(contract: Any, objs: list[Any], context: dict) -> list[Any]:
            try:
                prepared = self.computation.prepare_execution(
                    contract,
                    objs,
                    values=activity["call"]["parameters"],
                    context=context,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise self._ineligible(exc) from exc
            holder["prepared"] = prepared
            return list(objs)

        result = prepare_activity(
            self.ledger,
            activity_id,
            resolve_contract=self.resolve_contract,
            find_object=self.host.find_object,
            observe_object=self.facts,
            check_preconditions=self.computation.check_preconditions,
            decode_parameters=lambda _contract, values: values,
            state_status=self.state_status,
            apply_context=apply_context,
        )
        return result, holder.get("prepared")

    def _reference(self, activity: dict[str, Any]) -> tuple[dict | None, Any]:
        """Return the reference status and object of an activity's result."""
        output = next(
            (
                o
                for o in activity["outputs"]
                if "state_id" in o and o["role"] == "result"
            ),
            None,
        )
        if output is None:
            return None, None
        state = self.ledger.states[output["state_id"]]
        obj = self.host.find_object(state["object_uuid"])
        status = "available"
        if obj is None or self.state_status.get(state["state_id"]) == "unavailable":
            status, obj = "missing", None
        elif self.state_status.get(state["state_id"]) == "altered" or (
            self.facts(obj)["fingerprint"] != state["fingerprint"]
        ):
            status = "altered"
        return {"state_id": state["state_id"], "status": status}, obj

    def verify(
        self, activity_id: str, execute_candidate: Callable[[Any], Any]
    ) -> dict[str, Any]:
        """Recompute a recorded activity as a separate candidate and compare it.

        The workspace and the ledger never change; the verification run only
        appears in the returned report.

        Args:
            activity_id: Activity to verify.
            execute_candidate: ``(prepared execution) -> result``; runs the
             computation without inserting the result, and returns None on error
             or cancellation.

        Returns:
            Verification report.
        """
        activity = self.ledger.activity(activity_id)
        env_id = activity["environment_id"]
        environment = compare_environments(
            env_id,
            self.ledger.environments.get(env_id),
            environment_id(self.environment),
            self.environment,
        )
        reference, ref_obj = self._reference(activity)
        plan, prepared = self.prepare_replay(activity_id)
        if not isinstance(plan, Plan):
            return build_report(
                activity_id=activity_id,
                restoration=plan.restoration,
                eligibility=plan.eligibility,
                inputs=list(plan.input_statuses),
                reference=reference,
                environment=environment,
                reason=plan.reason,
                context=activity["context"],
            )
        inputs = [
            {"role": role, "state_id": state_id, "status": "available"}
            for role, state_id, _obj in plan.inputs
        ]
        candidate = execute_candidate(prepared)
        if candidate is None:
            return build_report(
                activity_id=activity_id,
                restoration="replayable",
                eligibility="ready",
                inputs=inputs,
                reference=reference,
                environment=environment,
                reason="The candidate computation failed or was cancelled",
                context=activity["context"],
            )
        comparison = None
        kind = self.host.object_kind(candidate)
        if ref_obj is not None and kind is not None:
            comparison = compare_rows(
                comparison_rows(candidate, kind),
                comparison_rows(ref_obj, self.host.object_kind(ref_obj)),
                self.computation.comparison_rule(plan.contract),
                environment=environment["match"],
            )
            if _units(candidate) != _units(ref_obj) or (
                kind != self.host.object_kind(ref_obj)
            ):
                comparison["verdict"] = "different"
        return build_report(
            activity_id=activity_id,
            restoration="replayable",
            eligibility="ready",
            inputs=inputs,
            reference=reference,
            environment=environment,
            comparison=comparison,
            candidate_state_ids=[("result", str(uuid.uuid4()))],
            context=activity["context"],
        )
