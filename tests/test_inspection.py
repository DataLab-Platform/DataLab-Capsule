# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for the read-only inspection of the provenance of a result."""

# pylint: disable=redefined-outer-name

from __future__ import annotations

import dataclasses
import uuid as uuidmod
from typing import Any

import numpy as np
import pytest

from datalab_capsule import inspection
from datalab_capsule.recorder import (
    Computation,
    Recorder,
    RecorderHost,
    StateUnavailableError,
)
from datalab_capsule.replay import IneligibleError

from .helpers import ROI, signal

UUIDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 400)]


class UnknownOperation(LookupError):
    """Fake error of the computation library."""


@dataclasses.dataclass
class FakeContract:
    """Fake operation contract."""

    function: Any
    qualified: bool = True
    pattern: str = "1_to_1"


@dataclasses.dataclass
class FakeTable:
    """Fake analysis result."""

    data: Any

    def to_dict(self) -> dict:
        """Stored form of the result."""
        return {"names": ["x"], "data": [[float(v)] for v in self.data]}


def gauss(src, sigma):
    """Fake smoothing (qualified)."""
    return signal(src.y * sigma, x=src.x)


def normalize(src, method):
    """Fake normalization (qualified)."""
    assert method == "maximum"
    return signal(src.y / np.max(np.abs(src.y)), x=src.x)


def difference(src, operand):
    """Fake difference (2 to 1, no parameters)."""
    return signal(src.y - operand.y, x=src.x)


def average(*sources):
    """Fake average (n to 1, no parameters)."""
    return signal(np.mean([s.y for s in sources], axis=0), x=sources[0].x)


def unqualified(src, offset):
    """Fake known operation without qualification."""
    return signal(src.y + offset, x=src.x)


def detect(src, threshold):
    """Fake analysis with an optional output."""
    found = src.x[src.y > threshold]
    return FakeTable(found) if threshold >= 0 else None


def plugin(src, offset):
    """Fake function unknown to the computation library."""
    return signal(src.y - offset, x=src.x)


OPERATIONS = {
    gauss: ("test.gauss", True, "1_to_1"),
    normalize: ("test.normalize", True, "1_to_1"),
    difference: ("test.difference", True, "2_to_1"),
    average: ("test.average", True, "n_to_1"),
    unqualified: ("test.unqualified", False, "1_to_1"),
    detect: ("test.detect", True, "1_to_0"),
}
CONTRACTS = {
    (op_id, 1): FakeContract(func, qualified, pattern)
    for func, (op_id, qualified, pattern) in OPERATIONS.items()
}


class Counters:
    """Calls made to the edition and to the computation library."""

    def __init__(self) -> None:
        self.facts = 0
        self.prepare = 0
        self.describe = 0

    def reset(self) -> None:
        """Reset every counter."""
        self.facts = self.prepare = self.describe = 0


COUNTERS = Counters()


def describe_call(func, inputs, param, *, roles=None, contract=...):
    """Fake description of a call."""
    COUNTERS.describe += 1
    entry = OPERATIONS.get(func) if contract is ... else None
    return {
        "operation": None if entry is None else {"id": entry[0], "contract_version": 1},
        "qualification": {"status": "qualified", "comparison": None},
        "pattern": None if entry is None else entry[2],
        "roles": list(roles or ["source"]),
        "parameters": {} if param is None else {"value": param},
        "parameter_codec": 2,
        "options": {},
        "metadata_read": (
            [{"role": "source", "position": 0, "key": "gain"}]
            if func is unqualified
            else []
        ),
        "limits": [],
        "implementation": {"package": "test", "version": "1", "python_name": "x"},
    }


def get_contract(operation_id, version):
    """Fake contract lookup."""
    try:
        return CONTRACTS[(operation_id, version)]
    except KeyError as exc:
        raise UnknownOperation(operation_id) from exc


def prepare_execution(contract, inputs, *, values, context):
    """Fake preparation (counted: inspection never prepares)."""
    COUNTERS.prepare += 1
    return (contract, inputs, values, context)


def computation() -> Computation:
    """Fake computation library."""
    return Computation(
        describe_call=describe_call,
        get_contract=get_contract,
        prepare_execution=prepare_execution,
        normalize_outputs=lambda _contract, result: [("result", result)],
        eligibility_code=lambda exc: (
            "unsupported_operation" if isinstance(exc, UnknownOperation) else None
        ),
    )


class Workspace:
    """Fake edition: objects by UUID, with metadata."""

    def __init__(self) -> None:
        self.objects: dict[str, Any] = {}
        self.unavailable: set[str] = set()
        self.count = 0

    def add(self, obj):
        """Insert *obj* under a new UUID."""
        obj.uuid = UUIDS[self.count]
        self.count += 1
        obj.metadata = getattr(obj, "metadata", {})
        self.objects[obj.uuid] = obj
        return obj

    def host(self) -> RecorderHost:
        """Return the recorder host of this workspace."""

        def facts(obj):
            COUNTERS.facts += 1
            if obj.uuid in self.unavailable:
                raise StateUnavailableError("spilled")
            from datalab_capsule.integrity import (  # pylint: disable=C0415
                state_facts,
            )

            return state_facts(obj)

        return RecorderHost(
            edition="desktop",
            edition_version=lambda: "1.4.0",
            object_uuid=lambda obj: getattr(obj, "uuid", None),
            find_object=self.objects.get,
            iter_objects=lambda: list(self.objects.values()),
            object_kind=lambda obj: "signal" if hasattr(obj, "y") else None,
            is_known_operation=lambda kind, name, origin: name == "gauss",
            state_facts=facts,
        )


@pytest.fixture
def workspace() -> Workspace:
    """Fake workspace."""
    return Workspace()


@pytest.fixture
def recorder(workspace) -> Recorder:
    """Recorder of the fake workspace."""
    COUNTERS.reset()
    return Recorder(workspace.host(), computation())


def run(recorder, workspace, func, param, *inputs, roles=None, command_id=None):
    """Run *func* as an ordinary processing and record it; return the output."""
    pending = recorder.begin(
        func,
        param,
        list(inputs) if len(inputs) > 1 else inputs[0],
        command_id=command_id,
        roles=roles,
    )
    args = inputs if param is None else (*inputs, param)
    output = workspace.add(func(*args))
    activity = recorder.complete(pending, output)
    assert activity is not None
    return output


def recompute_in_place(recorder, obj, func, param, *inputs):
    """Recompute *obj* in place from *inputs* and record it."""
    pending = recorder.begin(func, param, inputs[0], origin="recompute_in_place")
    obj.y = func(*inputs, param).y
    assert recorder.complete(pending, obj) is not None


@pytest.fixture
def chain(recorder, workspace):
    """S0 -> gauss(sigma=2) -> S1 -> normalize -> S2, then S1 recomputed (5)."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    s1 = run(recorder, workspace, gauss, 2.0, s0)
    s2 = run(recorder, workspace, normalize, "maximum", s1)
    recompute_in_place(recorder, s1, gauss, 5.0, s0)
    return s0, s1, s2


def test_sigma_scenario_describes_the_state_actually_used(recorder, chain) -> None:
    """S2 keeps the sigma=2 input even after S1 was recomputed with sigma=5."""
    _s0, s1, s2 = chain
    ledger_before = recorder.ledger.to_json()
    COUNTERS.reset()
    view = recorder.describe_object(s2.uuid)
    assert view["inspection"] == inspection.INSPECTION_VERSION
    assert view["object"] == {"object_uuid": s2.uuid, "present": True}
    assert view["anchor"]["content"] == "unchecked"
    assert view["anchor"]["latest"] is True
    production = view["production"]
    assert production["operation"] == {"id": "test.normalize", "contract_version": 1}
    assert production["parameters"] == {"value": "maximum"}
    assert production["support"]["function"] == "known"
    assert production["support"]["verification"] == "qualified"
    (source,) = production["inputs"]
    assert source["role"] == "source" and source["object_uuid"] == s1.uuid
    assert source["object_present"] is True and source["data"] == "unchecked"
    assert source["has_ancestors"] is True and source["cycle"] is False
    # Expanding the input reads the sigma=2 production, not S1's current content.
    ancestor = recorder.describe_state(
        source["state_id"], trail=[view["state"]["state_id"]]
    )
    assert ancestor["production"]["parameters"] == {"value": 2.0}
    assert ancestor["anchor"]["latest"] is False
    assert ancestor["object"]["object_uuid"] == s1.uuid
    current = recorder.describe_object(s1.uuid)
    assert current["production"]["parameters"] == {"value": 5.0}
    assert current["production"]["origin"] == "recompute_in_place"
    # Consultation: no fingerprint, no preparation, no description, no change.
    assert (COUNTERS.facts, COUNTERS.prepare, COUNTERS.describe) == (0, 0, 0)
    assert recorder.ledger.to_json() == ledger_before
    checked = recorder.describe_object(s2.uuid, check_data=True)
    assert checked["anchor"]["content"] == "matches"
    assert checked["production"]["inputs"][0]["data"] == "changed"
    assert checked["production"]["outputs"][0]["status"] == "available"
    assert COUNTERS.facts > 0 and COUNTERS.prepare == 0
    assert recorder.ledger.to_json() == ledger_before
    assert recorder.capture_failures == 0


def test_deleted_intermediate_keeps_its_description(recorder, workspace, chain):
    """A deleted input stays described and is never substituted."""
    _s0, s1, s2 = chain
    del workspace.objects[s1.uuid]
    view = recorder.describe_object(s2.uuid, check_data=True)
    (source,) = view["production"]["inputs"]
    assert source["object_present"] is False and source["data"] == "missing"
    ancestor = recorder.describe_state(source["state_id"])
    assert ancestor["object"] == {"object_uuid": s1.uuid, "present": False}
    assert ancestor["anchor"]["content"] == "unchecked"
    assert ancestor["production"]["parameters"] == {"value": 2.0}
    call = recorder.producing_call(s2.uuid)
    assert call["inputs"] == [
        {
            "role": "source",
            "state_id": source["state_id"],
            "object_uuid": s1.uuid,
            "present": False,
        }
    ]


def test_modified_output_and_presentation_only_changes(recorder, chain) -> None:
    """A changed output is not attributed; title and colour are not changes."""
    _s0, _s1, s2 = chain
    s2.title = "Renamed"
    s2.metadata["color"] = "red"
    view = recorder.describe_object(s2.uuid, check_data=True)
    assert view["anchor"]["content"] == "matches"
    s2.y = s2.y + 1.0
    view = recorder.describe_object(s2.uuid, check_data=True)
    assert view["anchor"]["content"] == "differs"
    assert view["production"]["outputs"][0]["status"] == "altered"
    assert view["production"]["operation"]["id"] == "test.normalize"
    s2.y = s2.y - 1.0
    s2.roi = ROI([0.0, 0.5])
    view = recorder.describe_object(s2.uuid, check_data=True)
    assert view["anchor"]["content"] == "differs"


def test_roles_order_and_repetitions(recorder, workspace) -> None:
    """Every input is listed with its role and position, repetitions included."""
    a = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    b = workspace.add(signal([4.0, 3.0, 2.0, 1.0]))
    c = run(recorder, workspace, gauss, 2.0, a)
    mean = run(recorder, workspace, average, None, a, b, a, roles=["sources"] * 3)
    pair = run(recorder, workspace, average, None, a, b, roles=["sources"] * 2)
    diff = run(recorder, workspace, difference, None, c, b, roles=["source", "operand"])
    inputs = recorder.describe_object(mean.uuid)["production"]["inputs"]
    assert [(i["position"], i["role"], i["object_uuid"]) for i in inputs] == [
        (0, "sources", a.uuid),
        (1, "sources", b.uuid),
        (2, "sources", a.uuid),
    ]
    assert recorder.describe_object(mean.uuid)["production"]["parameters"] == {}
    roles = [
        i["role"] for i in recorder.describe_object(pair.uuid)["production"]["inputs"]
    ]
    assert roles == ["sources", "sources"]
    branches = recorder.describe_object(diff.uuid)["production"]["inputs"]
    assert [(i["role"], i["has_ancestors"]) for i in branches] == [
        ("source", True),
        ("operand", False),
    ]
    call = recorder.producing_call(mean.uuid)
    assert [i["object_uuid"] for i in call["inputs"]] == [a.uuid, b.uuid, a.uuid]
    assert call["contract"].pattern == "n_to_1" and call["eligibility"] is None


def test_cycles_and_depth_are_reported(recorder, chain) -> None:
    """Expansion is protected against cycles and excessive depth."""
    _s0, _s1, s2 = chain
    view = recorder.describe_object(s2.uuid)
    input_state = view["production"]["inputs"][0]["state_id"]
    looped = recorder.describe_state(view["state"]["state_id"], trail=[input_state])
    assert looped["production"]["inputs"][0]["cycle"] is True
    deep = recorder.describe_state(
        view["state"]["state_id"], trail=["x"] * inspection.MAX_DEPTH
    )
    assert "depth_limit" in deep["limits"]
    assert deep["production"]["inputs"][0]["has_ancestors"] is False


def test_observed_state_has_no_producer(recorder, workspace) -> None:
    """A state observed after an untracked change has no recorded production."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    s1 = run(recorder, workspace, gauss, 2.0, s0)
    s1.y = s1.y * 3.0
    run(recorder, workspace, normalize, "maximum", s1)
    view = recorder.describe_object(s1.uuid)
    assert view["state"]["produced_by"] is None and view["production"] is None
    assert [s["produced_by"] is not None for s in view["object_states"]] == [
        False,
        True,
    ]
    assert recorder.producing_call(s1.uuid) is None


def test_opaque_and_legacy_records(recorder, workspace) -> None:
    """Unknown functions and old records stay inspectable, without identity."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    out = run(recorder, workspace, plugin, 1.0, s0)
    production = recorder.describe_object(out.uuid)["production"]
    assert production["operation"] is None and production["opaque"] is True
    assert production["support"]["function"] == "opaque"
    assert production["support"]["verification"] == "unavailable"
    assert production["implementation"]["python_name"] == "x"
    assert recorder.producing_call(out.uuid) is None
    old = workspace.add(signal([5.0, 6.0, 7.0, 8.0]))
    old.metadata["__processing_parameters"] = {
        "func_name": "gauss",
        "pattern": "1-to-1",
        "source_uuid": s0.uuid,
    }
    recorder.load(None, replaced=True, saved_version="1.2.0")
    view = recorder.describe_object(old.uuid)
    assert view["production"] is None
    (legacy,) = view["legacy"]
    assert legacy["origin"] == "legacy_metadata" and legacy["operation"] is None
    assert legacy["support"]["function"] == "legacy"
    assert legacy["inputs"] == [
        {
            "position": 0,
            "role": "source",
            "state_id": None,
            "object_uuid": s0.uuid,
            "object_present": True,
            "produced_by": None,
            "has_ancestors": False,
            "cycle": False,
            "data": "unknown",
        }
    ]
    assert legacy["legacy"]["observed_state_id"] == view["state"]["state_id"]
    assert "legacy_context_unknown" in legacy["limits"]


def test_analyses_are_listed_separately(recorder, workspace) -> None:
    """Analyses are not producers; their results have a status."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    pending = recorder.begin(detect, 2.0, s0)
    s0.metadata["_peaks_"] = detect(s0, 2.0).to_dict()
    recorder.complete_analysis(pending, s0, "table", "_peaks_")
    pending = recorder.begin(detect, -1.0, s0)
    assert detect(s0, -1.0) is None
    recorder.complete_without_result(pending)
    view = recorder.describe_object(s0.uuid)
    assert view["production"] is None
    found, nothing = view["analyses"]
    assert found["operation"]["id"] == "test.detect"
    assert found["analysed_state_id"] == view["state"]["state_id"]
    assert found["outputs"] == [
        {
            "role": "result",
            "recorded": "artifact",
            "kind": "table",
            "object_uuid": s0.uuid,
            "key": "_peaks_",
            "status": "unchecked",
        }
    ]
    assert found["no_result"] is False and nothing["no_result"] is True
    checked = recorder.describe_object(s0.uuid, check_data=True)
    assert checked["analyses"][0]["outputs"][0]["status"] == "available"
    s0.metadata["_peaks_"] = {"names": ["x"], "data": [[0.0]]}
    checked = recorder.describe_object(s0.uuid, check_data=True)
    assert checked["analyses"][0]["outputs"][0]["status"] == "replaced"
    del s0.metadata["_peaks_"]
    activity = recorder.describe_activity(found["activity_id"], check_data=True)
    assert activity["outputs"][0]["status"] == "missing"


def test_known_unqualified_and_unknown_operations(recorder, workspace) -> None:
    """Knowing a function and qualifying its verification are distinct."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    s0.metadata["gain"] = 2.0
    out = run(recorder, workspace, unqualified, 1.0, s0)
    production = recorder.describe_object(out.uuid)["production"]
    assert production["support"]["function"] == "known"
    assert production["support"]["verification"] == "not_qualified"
    assert production["support"]["reason"]
    assert production["context"]["metadata"] == [
        {"role": "source", "position": 0, "key": "gain"}
    ]
    call = recorder.producing_call(out.uuid)
    assert call["contract"] is CONTRACTS[("test.unqualified", 1)]
    with pytest.raises(IneligibleError):
        recorder.resolve_contract("test.unqualified", 1)
    assert recorder.resolve_function("test.unqualified", 1).qualified is False
    s0.metadata["gain"] = 3.0
    checked = recorder.describe_object(out.uuid, check_data=True)
    assert checked["production"]["context"]["metadata"][0]["status"] == "changed"
    activity = recorder.ledger.activity(production["activity_id"])
    activity["call"]["operation"] = {"id": "test.removed", "contract_version": 1}
    production = recorder.describe_object(out.uuid)["production"]
    assert production["support"]["function"] == "unknown"
    assert production["support"]["verification"] == "unavailable"
    call = recorder.producing_call(out.uuid)
    assert call["contract"] is None
    assert call["eligibility"] == "unsupported_operation"
    with pytest.raises(IneligibleError):
        recorder.resolve_function("test.removed", 1)


def test_unavailable_data_are_reported(recorder, workspace, chain) -> None:
    """Data that are not available locally get an explicit status."""
    _s0, s1, s2 = chain
    workspace.unavailable.update({s1.uuid, s2.uuid})
    view = recorder.describe_object(s2.uuid, check_data=True)
    assert view["anchor"]["content"] == "unavailable"
    assert view["production"]["inputs"][0]["data"] == "unavailable"
    assert view["production"]["outputs"][0]["status"] == "unavailable"
    assert recorder.capture_failures == 0


def test_file_status_and_unknown_objects(recorder, chain) -> None:
    """Statuses read in the file are reported; unknown objects are empty."""
    _s0, _s1, s2 = chain
    state_id = recorder.ledger.latest_state(s2.uuid)["state_id"]
    recorder.state_status[state_id] = "altered"
    view = recorder.describe_object(s2.uuid)
    assert view["anchor"]["file_status"] == "altered"
    view = recorder.describe_object(str(uuidmod.uuid4()))
    assert view["state"] is None and view["production"] is None
    assert view["object"]["present"] is False and view["anchor"]["content"] == (
        "unrecorded"
    )
    with pytest.raises(KeyError):
        recorder.describe_state("unknown")
    with pytest.raises(KeyError):
        recorder.describe_activity("unknown")


def test_inputs_are_bounded(recorder, workspace) -> None:
    """A long list of inputs is truncated and reported as such."""
    sources = [workspace.add(signal([float(i), 1.0, 2.0, 3.0])) for i in range(250)]
    mean = run(recorder, workspace, average, None, *sources, roles=["sources"] * 250)
    production = recorder.describe_object(mean.uuid)["production"]
    assert len(production["inputs"]) == inspection.MAX_INPUTS
    assert production["input_count"] == 250
    assert "inputs_truncated" in production["limits"]
    # Processing needs every input: the producing call is never truncated.
    assert len(recorder.producing_call(mean.uuid)["inputs"]) == 250


def test_command_and_deferred_activity_ids(recorder, workspace) -> None:
    """Activities of one command and of a deferred replay are listed exactly."""
    s0 = workspace.add(signal([1.0, 2.0, 3.0, 4.0]))
    command = str(uuidmod.uuid4())
    first = run(recorder, workspace, gauss, 2.0, s0, command_id=command)
    second = run(recorder, workspace, gauss, 3.0, s0, command_id=command)
    ids = recorder.command_activity_ids(command)
    assert ids == [
        recorder.ledger.latest_state(first.uuid)["produced_by"],
        recorder.ledger.latest_state(second.uuid)["produced_by"],
    ]
    assert recorder.command_activity_ids(str(uuidmod.uuid4())) == []
    with recorder.deferred("history_replay") as deferred:
        pending = recorder.begin(gauss, 4.0, s0)
        temp = workspace.add(gauss(s0, 4.0))
        assert recorder.complete(pending, temp) is None
        pending = recorder.begin(detect, 2.0, s0)
        s0.metadata["_peaks_"] = detect(s0, 2.0).to_dict()
        analysis = recorder.complete_analysis(pending, s0, "table", "_peaks_")
        pending = recorder.begin(detect, -1.0, s0)
        nothing = recorder.complete_without_result(pending)
    assert deferred.activity_ids == [analysis["activity_id"], nothing["activity_id"]]
    assert recorder.finalize_deferred(deferred, {temp.uuid: first.uuid}) == 1
    replayed = recorder.ledger.latest_state(first.uuid)["produced_by"]
    assert deferred.activity_ids[-1] == replayed
    assert recorder.ledger.activity(replayed)["origin"] == "history_replay"
