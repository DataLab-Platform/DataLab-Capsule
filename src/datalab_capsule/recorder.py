# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Provenance recorder shared by the DataLab editions.

The recorder holds the workspace ledger of one application and implements what
both editions need: building operation calls from the description of the
computation library, recording executions (``begin``, ``complete``,
``complete_analysis``, ``complete_without_result``, deferred finalization),
artifact statuses, replay preparation and verification of every output, and the
ledger lifecycle (reset, save, load, import of old workspaces).

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
    compare_artifacts,
    compare_environments,
    compare_rows,
)
from datalab_capsule.environment import collect_environment, environment_id
from datalab_capsule.hdf5 import load_ledger, read_capsule_origin, save_ledger
from datalab_capsule.inspection import (
    IDENTITY_LIMIT,
    Inspector,
    holder_of_artifact,
    holder_of_state,
    metadata_digest,
    output_state_status,
)
from datalab_capsule.integrity import (
    ProvenanceError,
    roi_facts,
    signal_rows,
    state_facts,
)
from datalab_capsule.ledger import Ledger, artifact_status, utc_timestamp
from datalab_capsule.legacy import (
    PROCESSING_KEY,
    LegacyObject,
    import_legacy,
    legacy_summary,
)
from datalab_capsule.replay import IneligibleError, Plan, Refusal, prepare_activity

__all__ = [
    "APPEND_NOTICE",
    "CandidateError",
    "Computation",
    "PendingActivity",
    "Recorder",
    "RecorderHost",
    "StateUnavailableError",
    "comparison_rows",
    "computation_from_sigima",
]

_logger = logging.getLogger(__name__)

#: Notice of :meth:`Recorder.load` when a file holding provenance is appended to
#: the current workspace (editions may show a translation before appending).
APPEND_NOTICE = (
    "The data are added, but their earlier provenance is not imported. Open the "
    "file replacing the current workspace to keep it."
)


class StateUnavailableError(ProvenanceError):
    """Raised by a host when the data of an object are not available to observe.

    For example, DataLab-Web raises it for objects whose arrays were spilled to
    disk to save memory.
    """


class CandidateError(ProvenanceError):
    """Raised by ``execute_candidate`` when a candidate computation fails.

    Also raised when it is cancelled. A candidate returning None is not a
    failure: the computation produced no result (e.g. a detection that found
    nothing), which is compared with what the activity recorded.
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
         description`` (see ``sigima.proc.contracts.describe_call``).
        get_contract: ``(operation_id, contract_version) -> contract``; raises
         when the operation or the version is unknown.
        prepare_execution: ``(contract, inputs, *, values, context) -> prepared``
         (see ``sigima.proc.contracts.prepare_execution``).
        normalize_outputs: ``(contract, result) -> [(role, value)]``.
        eligibility_code: Eligibility code of an error raised by the functions
         above, or None for an unexpected error.
        comparison_rule: ``(contract) -> rule``: comparison rule declared by the
         qualification, as plain data, or None.
        qualification_status: ``(contract) -> status``: ``qualified``,
         ``not_qualified`` or ``qualification_outdated``.
        artifact_value: ``(analysis result) -> stored form``: the form in which
         the editions store an analysis result in object metadata (by default,
         its ``to_dict()``), compared with the recorded one.
    """

    describe_call: Callable[..., dict[str, Any]]
    get_contract: Callable[[str, int], Any]
    prepare_execution: Callable[..., Any]
    normalize_outputs: Callable[[Any, Any], list[tuple[str, Any]]]
    eligibility_code: Callable[[BaseException], str | None]
    comparison_rule: Callable[[Any], dict[str, Any] | None] = lambda _contract: None
    qualification_status: Callable[[Any], str] = lambda contract: (
        "qualified" if contract.qualified else "not_qualified"
    )
    artifact_value: Callable[[Any], Any] = lambda result: result.to_dict()


def computation_from_sigima(contracts: Any) -> Computation:
    """Return the :class:`Computation` of Sigima, from its contracts package.

    The package ``sigima.proc.contracts`` is passed by the application; it is
    used by attribute, never imported here. This is the only place where Sigima
    errors are mapped to eligibility codes.
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
        if isinstance(exc, contracts.ExecutionError):
            return execution_codes.get(exc.code)
        return None

    return Computation(
        describe_call=contracts.describe_call,
        get_contract=contracts.get_operation_contract,
        prepare_execution=contracts.prepare_execution,
        normalize_outputs=contracts.normalize_outputs,
        eligibility_code=eligibility_code,
        comparison_rule=lambda contract: (
            None if contract.comparison is None else contract.comparison.to_dict()
        ),
        qualification_status=lambda contract: contract.qualification_status,
    )


def _metadata_digest(obj: Any, read: dict[str, Any]) -> str:
    """Return the digest of the metadata value *read* of *obj* (None if absent)."""
    return metadata_digest(obj, read["key"])


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


def _roi_digest(obj: Any) -> str | None:
    facts = roi_facts(getattr(obj, "roi", None))
    return None if facts is None else facts["digest"]


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
    """Pending activities held until temporary outputs get their final UUIDs.

    Attributes:
        origin: Origin of the activities recorded in this context.
        items: Held ``(pending activity, temporary output UUID)`` pairs.
        activity_ids: Identifiers of the activities recorded in this context, in
         recording order: analyses and executions without result (recorded at
         once), then the activities recorded by
         :meth:`Recorder.finalize_deferred`.
    """

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.items: list[tuple[PendingActivity, str]] = []
        self.activity_ids: list[str] = []


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
        #: ``{"fingerprint", "name", "attachments"}`` of the capsule the
        #: workspace was opened from (the parent of a revision exported later),
        #: saved with the workspace and restored when it is opened again.
        self.capsule_origin: dict[str, Any] | None = None
        self._index: Any = None

    # -- Workspace lifecycle ------------------------------------------------

    def reset(self) -> None:
        """Start an empty ledger for a new workspace (no capsule origin)."""
        self.ledger = Ledger()
        self.file_status = None
        self.state_status = {}
        self.notices = []
        self.capsule_origin = None
        self._deferred = None
        self._index = None

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
        """Write the ledger block (and the capsule origin) into a workspace file,
        after its panels."""
        save_ledger(h5file, self.ledger, self.capsule_origin)

    @staticmethod
    def read(
        h5file: Any,
    ) -> tuple[Ledger, dict[str, str], dict[str, Any] | None] | None:
        """Read and validate the block of a workspace file.

        Returns:
            ``(ledger, state_status, capsule_origin)``, or None without block.

        Raises:
            ProvenanceFormatError: If the block is invalid.
        """
        block = load_ledger(h5file)
        if block is None:
            return None
        return (*block, read_capsule_origin(h5file))

    def load(
        self,
        block: tuple[Any, ...] | None,
        replaced: bool,
        saved_version: str | None = None,
        current_version: str | None = None,
        appended: Iterable[Any] = (),
    ) -> None:
        """Adopt the ledger read from a workspace file, after its objects.

        Processing records of objects unknown to the ledger (workspaces saved
        without provenance) are then imported as ``legacy_metadata`` activities.

        Args:
            block: ``(ledger, state_status[, capsule_origin])`` read before the
             workspace was replaced (see :meth:`read`), or None when the file
             has no provenance block.
            replaced: False when the file was appended to the current workspace:
             ledgers are not merged and the file's block is ignored (the capsule
             origin of the current workspace is kept).
            saved_version: Application version that saved the file, as stored.
            current_version: Version compared with *saved_version*, or None.
            appended: Objects added to the workspace by an appended file. Those
             holding an identifier already known to the ledger (e.g. the
             identifier of a deleted object) get a new observed state marked
             with :data:`~datalab_capsule.inspection.IDENTITY_LIMIT`: nothing
             recorded before is attributed to them.
        """
        if not replaced:
            if block is not None:
                _logger.warning(APPEND_NOTICE)
                self.notices.append(APPEND_NOTICE)
            self._mark_appended(appended)
            return
        self.reset()
        if block is None:
            self.file_status = "absent"
        else:
            self.ledger, self.state_status = block[0], block[1]
            self.capsule_origin = block[2] if len(block) > 2 else None
            self.file_status = "loaded"
            self._index = None
        self._import_legacy(saved_version, current_version)

    def _mark_appended(self, objects: Iterable[Any]) -> None:
        """Break the identity of appended objects whose UUID the ledger knows."""
        try:
            known = set(self.ledger.object_uuids())
            # Objects bound by records of old workspaces have no state
            for activity in self.ledger.activities:
                for item in activity["call"]["inputs"]:
                    known.update(item["binding"].get("object_uuids") or ())
            for obj in objects:
                uid = self.host.object_uuid(obj)
                if (
                    uid is None
                    or uid not in known
                    or self.host.object_kind(obj) is None
                ):
                    continue
                try:
                    facts = self.facts(obj)
                except StateUnavailableError:
                    continue
                facts["limits"] = [*facts.get("limits", []), IDENTITY_LIMIT]
                self.ledger.observe(uid, facts, fresh=True)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)

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
        executed: Sequence[Any] | None = None,
    ) -> tuple[dict[str, Any], list[str], dict[str, Any], dict[str, Any]]:
        """Return the call, limits, implementation and context of an execution.

        The identity of the operation is kept as soon as the computation library
        knows it, whether or not its contract is qualified for replay: the
        qualification is checked when replaying, against the local library. The
        context holds the global options read by the computation, if any, and
        the digests of the metadata of inputs it reads (see
        :meth:`_metadata_read`), taken from *executed* when given.
        """
        description = self.computation.describe_call(func, objs, param, roles=roles)
        operation = description["operation"] or {}
        call = make_call(
            operation.get("id"),
            operation.get("contract_version"),
            description["parameters"],
            list(zip(description["roles"], state_ids)),
        )
        options = description.get("options")
        context: dict[str, Any] = {"options": dict(options)} if options else {}
        reads = description.get("metadata_read") or []
        if reads:
            sources = objs if executed is None else list(executed)
            context["metadata"] = [
                {**read, "digest": _metadata_digest(sources[read["position"]], read)}
                for read in reads
            ]
        return (
            call,
            list(description["limits"]),
            description["implementation"],
            context,
        )

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
        executed: Sequence[Any] | None = None,
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
             global options), if known before it runs; by default, the global
             options read by the computation (see ``describe_call``).
            executed: Objects actually passed to the computation, in the order
             of *source*, when they differ from the recorded inputs (e.g. copies
             whose metadata were converted before the call): the metadata read
             by the computation are taken from them.

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
            call, call_limits, implementation, described = self._build_call(
                func, param, state_ids, objs, roles, executed
            )
            pending = PendingActivity(
                call=call,
                implementation=implementation,
                limits=call_limits + list(limits),
                command_id=command_id,
                origin=origin,
                started_at=utc_timestamp(),
                context={**described, **(context or {})},
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
            activity = self._record(pending, [], origin, artifacts=[artifact])
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return None
        self._note_deferred(activity)
        return activity

    def _note_deferred(self, activity: dict[str, Any]) -> None:
        """List an activity recorded at once inside a deferred context."""
        if self._deferred is not None:
            self._deferred.activity_ids.append(activity["activity_id"])

    def complete_without_result(
        self, pending: PendingActivity | None
    ) -> dict[str, Any] | None:
        """Record a completed execution that produced no result.

        For a computation whose output is optional and which returned None
        without error (e.g. a detection that found nothing). An error or a
        cancellation is not recorded.

        Returns:
            The recorded activity (without outputs), or None.
        """
        if pending is None:
            return None
        try:
            origin = pending.origin if self._deferred is None else self._deferred.origin
            activity = self._record(pending, [], origin)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._capture_failed(exc)
            return None
        self._note_deferred(activity)
        return activity

    def artifact_statuses(self, activity_id: str) -> list[dict[str, str]]:
        """Return the status of each analysis result recorded by an activity.

        Returns:
            One ``{"object_uuid", "key", "status"}`` per artifact output;
            ``status`` is ``available`` (the object still holds this result),
            ``replaced`` (another result now holds the key) or ``missing``.
        """
        statuses = []
        activity = self.ledger.activity(activity_id)
        for output in activity["outputs"]:
            artifact = output.get("artifact")
            if artifact is None:
                continue
            obj = self._artifact_holder(activity, artifact)
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
            Number of recorded activities; their identifiers are appended to
            ``deferred.activity_ids``.
        """
        recorded = 0
        items, deferred.items = deferred.items, []
        for pending, temp_uuid in items:
            final_uuid = remap.get(temp_uuid)
            output = None if final_uuid is None else self.host.find_object(final_uuid)
            if self.host.object_kind(output) is None:
                continue
            try:
                activity = self._record(
                    pending,
                    [("result", final_uuid, self.facts(output))],
                    deferred.origin,
                )
                deferred.activity_ids.append(activity["activity_id"])
                recorded += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._capture_failed(exc)
        return recorded

    # -- Contracts ----------------------------------------------------------

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
        contract = self.resolve_function(operation_id, version)
        status = self.computation.qualification_status(contract)
        if status != "qualified":
            reason = {
                "qualification_outdated": "its qualification names another version",
            }.get(status, "it is not qualified yet")
            raise IneligibleError(
                "unsupported_contract",
                f"{operation_id} v{version} cannot be replayed: {reason}",
            )
        return contract

    def resolve_function(self, operation_id: str, version: int) -> Any:
        """Return the contract of a recorded operation known to the local library.

        Unlike :meth:`resolve_contract`, the qualification is not required: an
        ordinary processing (e.g. a re-application from Processing) may use any
        known operation; only the verification of a recorded activity needs a
        qualified contract.

        Raises:
            IneligibleError: ``unsupported_operation`` or ``unsupported_contract``.
        """
        try:
            return self.computation.get_contract(operation_id, version)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise self._ineligible(exc) from exc

    # -- Inspection ---------------------------------------------------------

    def _inspector(self) -> Inspector:
        """Return an inspector of the current ledger (lookups are cached)."""
        return Inspector(
            self.ledger,
            self.host,
            self.computation,
            self.state_status,
            StateUnavailableError,
            self._lookups(),
        )

    def describe_object(
        self, object_uuid: str, check_data: bool = False
    ) -> dict[str, Any]:
        """Describe the provenance of an object (see :mod:`.inspection`).

        Args:
            object_uuid: Object to describe, anchored on its latest recorded state.
            check_data: Compare the object and the direct inputs and outputs of
             its production with their recorded states; never computes.
        """
        return self._inspector().describe_object(object_uuid, check_data)

    def describe_state(
        self,
        state_id: str,
        check_data: bool = False,
        trail: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Describe a recorded state, e.g. an input to expand.

        Raises:
            KeyError: If the state does not exist.
        """
        return self._inspector().describe_state(state_id, check_data, trail)

    def describe_activity(
        self, activity_id: str, check_data: bool = False
    ) -> dict[str, Any]:
        """Describe one recorded activity (e.g. from a History action).

        Raises:
            KeyError: If the activity does not exist.
        """
        return self._inspector().describe_activity(activity_id, check_data)

    def producing_call(self, object_uuid: str) -> dict[str, Any] | None:
        """Return the call that produced the latest recorded state of an object.

        See :meth:`datalab_capsule.inspection.Inspector.producing_call`; the
        operation is resolved with :meth:`resolve_function`.
        """
        return self._inspector().producing_call(
            object_uuid, self.resolve_function, IneligibleError
        )

    def command_activity_ids(self, command_id: str) -> list[str]:
        """Return the activities recorded for one command, in recording order."""
        return self._inspector().command_activity_ids(command_id)

    # -- Replay -------------------------------------------------------------

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
            self._check_metadata_read(contract, objs, context)
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

        def observe(obj: Any) -> dict[str, Any]:
            try:
                return self.facts(obj)
            except StateUnavailableError as exc:
                raise IneligibleError(
                    "unsupported_context",
                    f"The data of an input are unavailable: {exc}",
                ) from exc

        result = prepare_activity(
            self.ledger,
            activity_id,
            resolve_contract=self.resolve_contract,
            find_object=self.host.find_object,
            observe_object=observe,
            check_preconditions=lambda _contract, _objects: None,
            decode_parameters=lambda _contract, values: values,
            state_status=self.state_status,
            apply_context=apply_context,
            find_state_object=self._state_holder,
        )
        return result, holder.get("prepared")

    def _lookups(self) -> Any:
        """Return the cached lookups of the ledger (identity breaks...)."""
        if not Inspector.index_is_current(self._index, self.ledger):
            self._index = Inspector.build_index(self.ledger)
        return self._index

    def _state_holder(self, state: dict[str, Any]) -> Any:
        """Return the live object of a recorded state (see ``holder_of_state``)."""
        return holder_of_state(self._lookups(), self.host.find_object, state)

    def _artifact_holder(
        self, activity: dict[str, Any], artifact: dict[str, Any]
    ) -> Any:
        """Return the live object holding an analysis result, or None."""
        return holder_of_artifact(
            self._lookups(), self.host.find_object, self.ledger, activity, artifact
        )

    def _check_metadata_read(
        self, contract: Any, objs: list[Any], context: dict[str, Any]
    ) -> None:
        """Check that the metadata read by the computation are those recorded.

        State fingerprints do not cover metadata: the computation library lists
        the metadata keys of inputs its contract reads (``metadata_read`` of
        ``describe_call``), and their digests recorded in the activity context
        must match the live inputs.

        Raises:
            IneligibleError: ``unsupported_context`` if they were not recorded,
             ``input_changed`` (with the changed inputs) if a value changed.
        """
        description = self.computation.describe_call(
            contract.function, objs, None, contract=contract
        )
        reads = description.get("metadata_read") or []
        if not reads:
            return
        recorded = {
            (item.get("position"), item.get("key")): item.get("digest")
            for item in context.get("metadata") or []
            if isinstance(item, dict)
        }
        changed = []
        for read in reads:
            where = (read["position"], read["key"])
            if where not in recorded:
                raise IneligibleError(
                    "unsupported_context",
                    f"Metadata {read['key']!r} of input {read['role']!r} read by "
                    "the computation were not recorded",
                )
            if _metadata_digest(objs[read["position"]], read) != recorded[where]:
                changed.append(read)
        if changed:
            names = ", ".join(f"{r['key']!r} of input {r['role']!r}" for r in changed)
            raise IneligibleError(
                "input_changed",
                f"Metadata read by the computation changed since the activity: {names}",
                changed=tuple(sorted({r["position"] for r in changed})),
            )

    def _references(self, activity: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the recorded outputs of an activity with their current status.

        Returns:
            One item per recorded output: ``role``, ``recorded`` (``state`` or
            ``artifact``), ``status`` and ``value`` (the reference object or
            stored analysis result when available, else None), plus
            ``state_id``, or ``artifact`` and ``kind``.
        """
        references = []
        for output in activity["outputs"]:
            if "state_id" in output:
                state = self.ledger.states[output["state_id"]]
                obj = self._state_holder(state)
                try:
                    status = output_state_status(
                        state, obj, self.facts, self.state_status.get(state["state_id"])
                    )
                except StateUnavailableError:
                    # The data of the result cannot be read: no reference, as
                    # for a state not saved in the file
                    status = "missing"
                references.append(
                    {
                        "role": output["role"],
                        "recorded": "state",
                        "state_id": state["state_id"],
                        "status": status,
                        "value": obj if status in ("available", "altered") else None,
                    }
                )
                continue
            artifact = output["artifact"]
            obj = self._artifact_holder(activity, artifact)
            value = None if obj is None else obj.metadata.get(artifact["key"])
            status = artifact_status(artifact, value)
            references.append(
                {
                    "role": output["role"],
                    "recorded": "artifact",
                    "artifact": {
                        "object_uuid": artifact["object_uuid"],
                        "key": artifact["key"],
                    },
                    "kind": artifact["kind"],
                    "status": status,
                    "value": value if status == "available" else None,
                }
            )
        return references

    def _compare_output(
        self,
        contract: Any,
        reference: dict[str, Any],
        value: Any,
        environment: str,
    ) -> dict[str, Any]:
        """Compare one candidate output with its recorded reference."""
        entry = {
            key: reference[key]
            for key in ("role", "recorded", "state_id", "artifact", "status")
            if key in reference
        }
        entry.update(verdict="not_verified", rule=None, observed=None, detail=None)
        if reference["recorded"] == "none" or value is None:
            if reference["recorded"] == "none" and value is None:
                entry["verdict"] = "exact"
            elif value is None:
                entry.update(verdict="different", detail="No result was produced")
            else:
                entry.update(
                    verdict="different",
                    detail="A result was produced where the activity recorded none",
                )
            return entry
        if reference["status"] != "available":
            entry["detail"] = f"The recorded output is {reference['status']}"
            return entry
        rule = self.computation.comparison_rule(contract)
        role = reference["role"]
        if reference["recorded"] == "artifact":
            comparison = compare_artifacts(
                reference["kind"],
                self.computation.artifact_value(value),
                reference["value"],
                rule,
                output=role,
                environment=environment,
            )
        else:
            ref_obj = reference["value"]
            kind, ref_kind = (
                self.host.object_kind(value),
                self.host.object_kind(ref_obj),
            )
            if kind != ref_kind:
                entry.update(
                    verdict="different",
                    detail=f"A {kind or type(value).__name__} was produced "
                    f"instead of a {ref_kind}",
                )
                return entry
            comparison = compare_rows(
                comparison_rows(value, kind),
                comparison_rows(ref_obj, ref_kind),
                rule,
                output=role,
                environment=environment,
            )
            roi_match = _roi_digest(value) == _roi_digest(ref_obj)
            comparison["observed"]["roi_match"] = roi_match
            if not roi_match or _units(value) != _units(ref_obj):
                comparison["verdict"] = "different"
        entry.update(comparison)
        return entry

    def verify(
        self, activity_id: str, execute_candidate: Callable[[Any], Any]
    ) -> dict[str, Any]:
        """Recompute a recorded activity as a separate candidate and compare it.

        Every output of the operation is compared with the recorded one, by
        role: signals and images (data, coordinates, ROI, units), analysis
        results (structure and values) and the absence of result of an optional
        output. The workspace and the ledger never change; the verification run
        only appears in the returned report.

        Args:
            activity_id: Activity to verify.
            execute_candidate: ``(prepared execution) -> result``; runs the
             computation without inserting the result and returns what the
             function returned (None when it produced no result); raises
             :class:`CandidateError` on error or cancellation.

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
        references = self._references(activity)
        reference = next(
            (
                {"state_id": ref["state_id"], "status": ref["status"]}
                for ref in references
                if ref["recorded"] == "state"
            ),
            None,
        )
        plan, prepared = self.prepare_replay(activity_id)
        report = {
            "activity_id": activity_id,
            "reference": reference,
            "environment": environment,
            "context": activity["context"],
        }
        if not isinstance(plan, Plan):
            return build_report(
                restoration=plan.restoration,
                eligibility=plan.eligibility,
                inputs=list(plan.input_statuses),
                reason=plan.reason,
                **report,
            )
        report.update(
            restoration="replayable",
            eligibility="ready",
            inputs=[
                {"role": role, "state_id": state_id, "status": "available"}
                for role, state_id, _obj in plan.inputs
            ],
        )
        try:
            candidate = execute_candidate(prepared)
        except CandidateError as exc:
            return build_report(
                reason=f"The candidate computation failed or was cancelled: {exc}",
                **report,
            )
        try:
            produced = self.computation.normalize_outputs(plan.contract, candidate)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return build_report(
                reason=f"The candidate result does not match the contract: {exc}",
                **report,
            )
        recorded = {ref["role"]: ref for ref in references}
        outputs = []
        candidate_ids = []
        for role, value in produced:
            ref = recorded.get(role, {"role": role, "recorded": "none"})
            ref.setdefault("status", "available")
            outputs.append(
                self._compare_output(plan.contract, ref, value, environment["match"])
            )
            if value is not None and self.host.object_kind(value) is not None:
                candidate_ids.append((role, str(uuid.uuid4())))
        return build_report(
            outputs=outputs,
            candidate_state_ids=candidate_ids,
            **report,
        )
