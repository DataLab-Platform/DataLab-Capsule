# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for the provenance recorder shared by the editions (fake host and library)."""

# pylint: disable=redefined-outer-name

from __future__ import annotations

import dataclasses
import io
from types import SimpleNamespace
from typing import Any

import h5py
import numpy as np
import pytest

from datalab_capsule.recorder import (
    APPEND_NOTICE,
    CandidateError,
    Computation,
    Recorder,
    RecorderHost,
    StateUnavailableError,
    computation_from_sigima,
)
from datalab_capsule.replay import Plan, Refusal

from .helpers import ROI, signal

UUIDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 20)]


class UnknownOperation(LookupError):
    """Fake error of the computation library."""


class BadParameters(ValueError):
    """Fake error of the computation library."""


@dataclasses.dataclass
class FakeContract:
    """Fake operation contract."""

    function: Any
    qualified: bool = True
    rule: dict | None = None


@dataclasses.dataclass
class FakePrepared:
    """Fake prepared execution."""

    function: Any
    args: tuple
    context: dict

    def run(self):
        """Run the function."""
        return self.function(*self.args)


def scale(src, factor):
    """Fake qualified operation: multiply Y by a factor."""
    out = signal(src.y * factor, x=src.x)
    return out


def shift(src, offset):
    """Fake operation without qualification."""
    return signal(src.y + offset, x=src.x)


def plugin(src, offset):
    """Fake function unknown to the computation library (e.g. a plugin)."""
    return signal(src.y - offset, x=src.x)


@dataclasses.dataclass
class FakeTable:
    """Fake analysis result."""

    data: Any

    def to_dict(self) -> dict:
        """Stored form of the result."""
        return {
            "title": "Peaks",
            "kind": "custom",
            "names": ["x"],
            "data": [[float(v)] for v in self.data],
            "roi_indices": None,
            "func_name": "detect",
            "attrs": {},
        }


def gain(src, factor):
    """Fake operation reading a metadata value of its input ("gain")."""
    return signal(src.y * factor * src.metadata["gain"], x=src.x)


def detect(src, threshold):
    """Fake analysis with an optional output: positions above a threshold."""
    found = src.x[src.y > threshold]
    return FakeTable(found) if threshold >= 0 else None


CONTRACTS = {
    ("test.scale", 1): FakeContract(scale),
    ("test.shift", 1): FakeContract(shift, qualified=False),
    ("test.detect", 1): FakeContract(detect),
    ("test.gain", 1): FakeContract(gain),
}


def describe_call(func, inputs, param, *, roles=None, contract=...):
    """Fake description of a call."""
    known = {
        scale: "test.scale",
        shift: "test.shift",
        detect: "test.detect",
        gain: "test.gain",
    }
    identified = contract is ... and func in known
    return {
        "operation": {"id": known[func], "contract_version": 1} if identified else None,
        "qualification": {
            "status": (
                "qualified"
                if identified and CONTRACTS[(known[func], 1)].qualified
                else "not_qualified"
            ),
            "comparison": None,
        },
        "pattern": "1_to_1",
        "roles": list(roles or ["source"]),
        "parameters": None if param == "unencodable" else {"factor": param},
        "parameter_codec": 2,
        "options": {"o": False} if func is shift else {},
        "metadata_read": (
            [{"role": "source", "position": 0, "key": "gain"}] if func is gain else []
        ),
        "limits": ["parameters_not_encoded"] if param == "unencodable" else [],
        "implementation": {"package": "test", "version": "1", "python_name": "x"},
    }


def get_contract(operation_id, version):
    """Fake contract lookup."""
    try:
        return CONTRACTS[(operation_id, version)]
    except KeyError as exc:
        raise UnknownOperation(operation_id) from exc


def prepare_execution(contract, inputs, *, values, context):
    """Fake preparation of a replay."""
    if not isinstance(values.get("factor"), (int, float)):
        raise BadParameters("factor must be a number")
    return FakePrepared(contract.function, (*inputs, values["factor"]), context)


def _normalize_outputs(_contract, result):
    """Fake output normalization: a text is not a valid output."""
    if isinstance(result, str):
        raise TypeError("unexpected output")
    return [("result", result)]


def computation() -> Computation:
    """Fake computation library."""
    codes = {
        UnknownOperation: "unsupported_operation",
        BadParameters: "invalid_parameters",
    }
    return Computation(
        describe_call=describe_call,
        get_contract=get_contract,
        prepare_execution=prepare_execution,
        normalize_outputs=_normalize_outputs,
        eligibility_code=lambda exc: codes.get(type(exc)),
        comparison_rule=lambda contract: contract.rule,
    )


class Workspace:
    """Fake edition: objects by UUID, with metadata."""

    def __init__(self) -> None:
        self.objects: dict[str, Any] = {}
        self.unavailable: set[str] = set()

    def add(self, obj, index: int):
        """Insert *obj* under UUID number *index*."""
        obj.uuid = UUIDS[index]
        obj.metadata = getattr(obj, "metadata", {})
        self.objects[obj.uuid] = obj
        return obj

    def host(self) -> RecorderHost:
        """Return the recorder host of this workspace."""

        def facts(obj):
            if obj.uuid in self.unavailable:
                raise StateUnavailableError("spilled")
            from datalab_capsule.integrity import state_facts

            return state_facts(obj)

        return RecorderHost(
            edition="desktop",
            edition_version=lambda: "1.4.0",
            object_uuid=lambda obj: getattr(obj, "uuid", None),
            find_object=self.objects.get,
            iter_objects=lambda: list(self.objects.values()),
            object_kind=lambda obj: "signal" if hasattr(obj, "y") else None,
            is_known_operation=lambda kind, name, origin: name == "scale",
            state_facts=facts,
        )


@pytest.fixture
def workspace() -> Workspace:
    """Fake workspace."""
    return Workspace()


def _run(recorder: Recorder, workspace: Workspace, func, factor, index=1):
    src = workspace.objects[UUIDS[0]]
    pending = recorder.begin(func, factor, src, command_id=UUIDS[18])
    result = workspace.add(func(src, factor), index)
    return pending, recorder.complete(pending, result)


def test_capture_qualified_and_opaque_calls(workspace) -> None:
    """Qualified calls keep their identity; others are opaque (for now)."""
    recorder = Recorder(workspace.host(), computation())
    workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _pending, activity = _run(recorder, workspace, scale, 2.0)
    assert activity["call"]["operation"] == {"id": "test.scale", "contract_version": 1}
    assert activity["call"]["parameters"] == {"factor": 2.0}
    assert activity["context"] == {"roi": None, "mask": None, "x_alignment": None}
    assert activity["edition"] == "desktop" and activity["command_id"] == UUIDS[18]
    _pending, activity = _run(recorder, workspace, shift, 1.0, index=2)
    assert activity["call"]["operation"] == {"id": "test.shift", "contract_version": 1}
    # The options read by the computation are recorded from its description.
    assert activity["context"]["options"] == {"o": False}
    _pending, activity = _run(recorder, workspace, plugin, 1.0, index=3)
    assert activity["call"]["operation"] is None
    assert activity["implementation"]["python_name"] == "x"
    src = workspace.objects[UUIDS[0]]
    pending = recorder.begin(shift, "unencodable", src)
    assert pending.limits == ["parameters_not_encoded"]
    assert pending.call["parameters"] is None
    assert pending.call["operation"]["id"] == "test.shift"
    context = {"x_alignment": {"rule": "r", "version": 1}, "options": {"o": True}}
    pending = recorder.begin(scale, 2.0, src, context=context)
    activity = recorder.complete(pending, workspace.add(scale(src, 2.0), 4))
    assert activity["context"]["options"] == {"o": True}
    assert activity["context"]["x_alignment"] == {"rule": "r", "version": 1}
    assert recorder.capture_failures == 0


def test_capture_is_skipped_or_isolated(workspace) -> None:
    """Non-objects, unknown roles and absent outputs are not recorded."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    assert recorder.begin(scale, 2.0, ["not an object"]) is None
    assert recorder.begin(scale, 2.0, [src, src, src]) is None
    pending = recorder.begin(scale, 2.0, src)
    assert recorder.complete(pending, scale(src, 2.0)) is None  # not inserted
    assert recorder.complete(None, src) is None
    workspace.unavailable.add(src.uuid)
    assert recorder.begin(scale, 2.0, src) is None
    assert recorder.capture_failures == 1


def test_deferred_activities_are_remapped(workspace) -> None:
    """Held activities are recorded under their final UUIDs, or dropped."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    with recorder.deferred("history_replay") as deferred:
        for index in (1, 2):
            pending = recorder.begin(scale, 2.0, src)
            output = workspace.add(scale(src, 2.0), index)
            assert recorder.complete(pending, output) is None
    final = workspace.add(scale(src, 2.0), 3)
    assert recorder.finalize_deferred(deferred, {UUIDS[1]: final.uuid}) == 1
    (activity,) = recorder.ledger.activities
    assert activity["origin"] == "history_replay"
    state = recorder.ledger.states[activity["outputs"][0]["state_id"]]
    assert state["object_uuid"] == final.uuid


def test_analysis_artifacts(workspace) -> None:
    """Analysis results are recorded by digest; their status follows the object."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    src.metadata["_result_"] = {"value": [1.5]}
    pending = recorder.begin(scale, 2.0, src)
    activity = recorder.complete_analysis(pending, src, "table", "_result_")
    activity_id = activity["activity_id"]
    assert recorder.artifact_statuses(activity_id)[0]["status"] == "available"
    src.metadata["_result_"] = {"value": [2.5]}
    assert recorder.artifact_statuses(activity_id)[0]["status"] == "replaced"
    del workspace.objects[src.uuid]
    assert recorder.artifact_statuses(activity_id)[0]["status"] == "missing"


def test_verify(workspace) -> None:
    """Verification prepares, runs a candidate and compares with the rule."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _pending, activity = _run(recorder, workspace, scale, 2.0)
    activity_id = activity["activity_id"]
    plan, prepared = recorder.prepare_replay(activity_id)
    assert isinstance(plan, Plan) and isinstance(prepared, FakePrepared)
    assert isinstance(recorder.prepare(activity_id), Plan)
    report = recorder.verify(activity_id, lambda p: p.run())
    assert (report["eligibility"], report["verdict"]) == ("ready", "exact")
    assert report["rule"] == {"kind": "exact"}
    absent = recorder.verify(activity_id, lambda p: None)
    assert absent["verdict"] == "different"
    assert absent["outputs"][0]["detail"] == "No result was produced"

    def cancelled(_prepared):
        raise CandidateError("cancelled")

    failed = recorder.verify(activity_id, cancelled)
    assert failed["verdict"] == "not_verified" and "failed" in failed["reason"]
    assert "outputs" not in failed
    unexpected = recorder.verify(activity_id, lambda p: "text")
    assert unexpected["verdict"] == "not_verified"
    assert "does not match" in unexpected["reason"]
    image = recorder.verify(activity_id, lambda p: FakeTable([1.0]))
    assert (
        image["verdict"] == "different"
        and "instead of" in (image["outputs"][0]["detail"])
    )
    CONTRACTS[("test.scale", 1)].rule = {
        "rule": "tolerance",
        "outputs": {"result": {"atol": 1e-6, "rtol": 0.0}},
        "same_environment": "tolerance",
        "justification": "test",
    }
    try:
        noisy = recorder.verify(
            activity_id,
            lambda p: signal(p.run().y + 1e-9, x=p.run().x),
        )
    finally:
        CONTRACTS[("test.scale", 1)].rule = None
    assert noisy["verdict"] == "within_tolerance"
    shifted_roi = recorder.verify(
        activity_id, lambda p: _with_roi(p.run(), ROI([0.0, 0.5]))
    )
    assert shifted_roi["verdict"] == "different"
    assert shifted_roi["observed"]["roi_match"] is False
    src.y = src.y + 1.0
    changed = recorder.verify(activity_id, lambda p: p.run())
    assert changed["eligibility"] == "input_changed"


def test_verify_with_unavailable_data(workspace) -> None:
    """Data that cannot be read refuse or skip the comparison, never crash."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _pending, activity = _run(recorder, workspace, scale, 2.0)
    activity_id = activity["activity_id"]
    result = workspace.objects[UUIDS[1]]
    workspace.unavailable.add(result.uuid)
    report = recorder.verify(activity_id, lambda p: p.run())
    assert report["eligibility"] == "ready"
    assert report["verdict"] == "not_verified"
    workspace.unavailable = {src.uuid}
    refused = recorder.prepare(activity_id)
    assert isinstance(refused, Refusal)
    assert refused.eligibility == "unsupported_context"
    assert recorder.verify(activity_id, lambda p: p.run())["verdict"] == "not_verified"


def _with_roi(obj, roi):
    obj.roi = roi
    return obj


@pytest.mark.parametrize(
    ("operation", "parameters", "code"),
    [
        (
            {"id": "test.unknown", "contract_version": 1},
            {"factor": 2.0},
            "unsupported_operation",
        ),
        (
            {"id": "test.shift", "contract_version": 1},
            {"factor": 2.0},
            "unsupported_contract",
        ),
        (
            {"id": "test.scale", "contract_version": 1},
            {"factor": "x"},
            "invalid_parameters",
        ),
    ],
)
def test_refusals_use_one_mapping(workspace, operation, parameters, code) -> None:
    """Errors of the library are mapped once to eligibility codes."""
    recorder = Recorder(workspace.host(), computation())
    workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _pending, activity = _run(recorder, workspace, scale, 2.0)
    activity["call"]["operation"] = operation
    activity["call"]["parameters"] = parameters
    plan, prepared = recorder.prepare_replay(activity["activity_id"])
    assert isinstance(plan, Refusal) and prepared is None
    assert plan.eligibility == code


def test_lifecycle_and_legacy_import(workspace) -> None:
    """Load adopts a ledger, imports old records and keeps notices."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _run(recorder, workspace, scale, 2.0)
    with h5py.File(io.BytesIO(), "w") as h5file:
        recorder.save(h5file)
        assert recorder.read(h5file) is not None
    recorder.load(None, replaced=False)
    assert recorder.notices == [] and recorder.file_status is None
    recorder.load((recorder.ledger, {}), replaced=False)
    assert recorder.notices == [APPEND_NOTICE]
    old = workspace.add(signal([5.0, 6.0, 7.0, 8.0]), 5)
    old.metadata["__processing_parameters"] = {
        "func_name": "scale",
        "pattern": "1-to-1",
        "source_uuid": src.uuid,
    }
    recorder.load(None, replaced=True, saved_version="1.2.0", current_version="1.4.0")
    assert recorder.file_status == "absent"
    assert len(recorder.ledger.activities) == 1
    assert recorder.legacy_summary()
    assert "legacy" in recorder.notices[0]
    workspace.unavailable.add(old.uuid)
    recorder.load(None, replaced=True)
    assert len(recorder.ledger.activities) == 1
    assert recorder.capture_failures == 0
    assert recorder.environment["edition"] == "desktop"
    recorder.invalidate_environment()
    assert recorder.environment["edition_version"] == "1.4.0"
    recorder.reset()
    assert not recorder.ledger.activities


def test_capsule_origin_survives_the_workspace_file(workspace) -> None:
    """Saving and opening the workspace again keeps the parent capsule."""
    origin = {
        "fingerprint": "sha256:" + "b" * 64,
        "name": "parent",
        "attachments": [],
    }
    recorder = Recorder(workspace.host(), computation())
    workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    _run(recorder, workspace, scale, 2.0)
    recorder.capsule_origin = dict(origin)
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as h5file:
        recorder.save(h5file)
        block = recorder.read(h5file)
    assert block[2] == origin
    # Opened again, replacing the workspace: the origin is restored.
    recorder.reset()
    assert recorder.capsule_origin is None
    recorder.load(block, replaced=True)
    assert recorder.capsule_origin == origin
    # Appended to a workspace: the origin of the current workspace is kept.
    other = {**origin, "fingerprint": "sha256:" + "c" * 64}
    recorder.load((block[0], block[1], other), replaced=False)
    assert recorder.capsule_origin == origin
    # A file without origin (or a block without it) opens without parent.
    recorder.load(block[:2], replaced=True)
    assert recorder.capsule_origin is None
    recorder.capsule_origin = dict(origin)
    recorder.load(None, replaced=True)
    assert recorder.capsule_origin is None


def test_recorder_reads_images(workspace) -> None:
    """Images are compared on their data and coordinates."""
    from datalab_capsule.recorder import comparison_rows

    image = SimpleNamespace(
        data=np.ones((2, 2)),
        is_uniform_coords=True,
        x0=0.0,
        y0=1.0,
        dx=0.5,
        dy=0.5,
    )
    rows = comparison_rows(image, "image")
    assert list(rows) == ["data", "x0", "y0", "dx", "dy"]
    image.is_uniform_coords = False
    image.xcoords, image.ycoords = [0.0, 2.0], [1.0, 3.0]
    assert list(comparison_rows(image, "image")) == ["data", "xcoords", "ycoords"]


def test_verify_analysis_results(workspace) -> None:
    """Analysis results are recomputed and compared with the stored ones."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    pending = recorder.begin(detect, 2.0, src)
    src.metadata["_detect_"] = {
        **detect(src, 2.0).to_dict(),
        "attrs": {"param_json": "{}"},
    }
    activity = recorder.complete_analysis(pending, src, "table", "_detect_")
    activity_id = activity["activity_id"]
    report = recorder.verify(activity_id, lambda p: p.run())
    assert report["verdict"] == "exact" and report["reference"] is None
    (output,) = report["outputs"]
    assert output["recorded"] == "artifact" and output["status"] == "available"
    assert output["artifact"] == {"object_uuid": src.uuid, "key": "_detect_"}
    assert output["observed"]["structure_match"] is True
    assert report["verification_activity"]["outputs"] == []
    moved = recorder.verify(activity_id, lambda p: FakeTable(p.run().data * 2))
    assert moved["verdict"] == "different"
    assert moved["outputs"][0]["observed"]["mismatches"] == 2
    absent = recorder.verify(activity_id, lambda p: None)
    assert absent["verdict"] == "different"
    src.metadata["_detect_"] = FakeTable([0.0]).to_dict()
    replaced = recorder.verify(activity_id, lambda p: p.run())
    assert replaced["verdict"] == "not_verified"
    assert replaced["outputs"][0]["status"] == "replaced"
    assert "replaced" in replaced["outputs"][0]["detail"]
    assert src.metadata["_detect_"] == FakeTable([0.0]).to_dict()  # unchanged


def test_execution_without_result(workspace) -> None:
    """A normal absence of result is recorded and verified, unlike a failure."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    pending = recorder.begin(detect, -1.0, src, command_id=UUIDS[18])
    assert detect(src, -1.0) is None
    activity = recorder.complete_without_result(pending)
    assert activity["outputs"] == [] and activity["command_id"] == UUIDS[18]
    assert recorder.complete_without_result(None) is None
    report = recorder.verify(activity["activity_id"], lambda p: p.run())
    assert report["verdict"] == "exact"
    assert report["outputs"][0]["recorded"] == "none"
    produced = recorder.verify(activity["activity_id"], lambda p: FakeTable([1.0]))
    assert produced["verdict"] == "different"
    assert "recorded none" in produced["outputs"][0]["detail"]
    with recorder.deferred("history_replay"):
        pending = recorder.begin(detect, -1.0, src)
        held = recorder.complete_without_result(pending)
    assert held["origin"] == "history_replay"


def test_computation_from_the_sigima_contracts_package() -> None:
    """One package gives the functions and the single error mapping."""

    class ContractError(Exception):
        """Fake base error of the contracts package."""

    errors = {
        name: type(name, (ContractError,), {})
        for name in (
            "UnknownOperationError",
            "IncompatibleContractError",
            "InvalidParametersError",
            "XAlignmentError",
        )
    }

    class ExecutionError(ContractError):
        """Fake execution error with a stable code."""

        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.code = code

    package = SimpleNamespace(
        describe_call=describe_call,
        get_operation_contract=get_contract,
        prepare_execution=prepare_execution,
        normalize_outputs=_normalize_outputs,
        ExecutionError=ExecutionError,
        **errors,
    )
    computed = computation_from_sigima(package)
    assert computed.describe_call is describe_call
    assert computed.get_contract is get_contract
    assert computed.prepare_execution is prepare_execution
    codes = {
        "UnknownOperationError": "unsupported_operation",
        "IncompatibleContractError": "unsupported_contract",
        "InvalidParametersError": "invalid_parameters",
        "XAlignmentError": "unsupported_context",
    }
    for name, code in codes.items():
        assert computed.eligibility_code(errors[name]()) == code
    assert computed.eligibility_code(ExecutionError("input_mismatch")) == (
        "unsupported_context"
    )
    assert computed.eligibility_code(ExecutionError("unexpected_output")) is None
    assert computed.eligibility_code(ValueError()) is None
    contract = SimpleNamespace(comparison=None, qualification_status="qualified")
    assert computed.comparison_rule(contract) is None
    assert computed.qualification_status(contract) == "qualified"


def test_metadata_read_by_the_computation(workspace) -> None:
    """Metadata read by a computation are recorded and checked before a replay."""
    recorder = Recorder(workspace.host(), computation())
    src = workspace.add(signal([1.0, 2.0, 3.0, 4.0]), 0)
    src.metadata["gain"] = 2.0
    pending = recorder.begin(gain, 3.0, src)
    activity = recorder.complete(pending, workspace.add(gain(src, 3.0), 1))
    (read,) = activity["context"]["metadata"]
    assert (read["role"], read["position"], read["key"]) == ("source", 0, "gain")
    assert read["digest"].startswith("sha256:")
    activity_id = activity["activity_id"]
    assert recorder.verify(activity_id, lambda p: p.run())["verdict"] == "exact"
    # The data are unchanged, so the state is the same: the metadata check
    # tells why the replay is not faithful.
    src.metadata["gain"] = 5.0
    changed = recorder.verify(activity_id, lambda p: p.run())
    assert changed["eligibility"] == "input_changed"
    assert "'gain' of input 'source'" in changed["reason"]
    assert [i["status"] for i in changed["inputs"]] == ["changed"]
    src.metadata["gain"] = 2.0
    activity["context"].pop("metadata")
    missing = recorder.verify(activity_id, lambda p: p.run())
    assert missing["eligibility"] == "unsupported_context"
    # The value actually used (e.g. converted on a copy) is the one recorded.
    used = signal([1.0, 2.0, 3.0, 4.0])
    used.metadata = {"gain": 7.0}
    pending = recorder.begin(gain, 3.0, src, executed=[used])
    assert pending.context["metadata"][0]["digest"] != read["digest"]
