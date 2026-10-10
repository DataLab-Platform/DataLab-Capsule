# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for comparison rules, reports and replay preparation."""

from __future__ import annotations

import numpy as np
import pytest

from datalab_capsule.calls import make_call
from datalab_capsule.compare import (
    ReportError,
    build_report,
    compare_environments,
    compare_exact,
)
from datalab_capsule.environment import collect_environment, environment_id
from datalab_capsule.integrity import signal_state_facts
from datalab_capsule.ledger import Ledger
from datalab_capsule.replay import IneligibleError, Plan, Refusal, prepare_activity

from .helpers import ROI, signal

SRC_UUID = "00000000-0000-4000-8000-000000000001"
DST_UUID = "00000000-0000-4000-8000-000000000002"


def _rows(sig) -> dict:
    return {"x": sig.x, "y": sig.y}


def test_compare_exact() -> None:
    """Exact equality, NaN positions, mismatch counts and errors."""
    ref = {"x": np.array([0.0, 1.0]), "y": np.array([1.0, np.nan])}
    assert compare_exact(ref, ref)["verdict"] == "exact"
    cand = {"x": np.array([0.0, 1.0]), "y": np.array([1.5, np.nan])}
    result = compare_exact(cand, ref)
    assert result["verdict"] == "different"
    assert result["observed"]["mismatches"] == 1
    assert result["observed"]["max_abs_error"] == 0.5
    assert result["observed"]["max_rel_error"] == 0.5
    nan_moved = {"x": np.array([0.0, 1.0]), "y": np.array([np.nan, 1.0])}
    assert compare_exact(nan_moved, ref)["observed"]["mismatches"] == 2
    float32 = {k: v.astype(np.float32) for k, v in ref.items()}
    assert compare_exact(float32, ref)["verdict"] == "different"
    short = {"x": np.array([0.0]), "y": np.array([1.0])}
    assert compare_exact(short, ref)["observed"]["shape_match"] is False
    zero_ref = {"y": np.array([0.0])}
    observed = compare_exact({"y": np.array([1.0])}, zero_ref)["observed"]
    assert observed["max_rel_error"] is None and observed["max_abs_error"] == 1.0


def test_environment_comparison() -> None:
    """Differences are listed by key; packages by name."""
    rec = collect_environment("desktop", "1.4.0", packages=("numpy",))
    cur = dict(rec, edition="web", packages={"numpy": "0.0.0"})
    result = compare_environments(environment_id(rec), rec, environment_id(cur), cur)
    assert result["match"] == "different"
    assert result["differences"]["edition"] == ["desktop", "web"]
    assert result["differences"]["numpy"][1] == "0.0.0"
    same = compare_environments("a", rec, "a", rec)
    assert same["match"] == "same" and same["differences"] == {}


def test_report_without_reference_is_not_verified() -> None:
    """A missing reference gives not_verified, even with a comparison."""
    env = compare_environments(None, None, None, None)
    comparison = compare_exact({"y": np.zeros(2)}, {"y": np.zeros(2)})
    report = build_report(
        activity_id="a",
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference={"state_id": "s", "status": "missing"},
        environment=env,
        comparison=comparison,
        candidate_state_ids=[("result", "c")],
    )
    assert report["verdict"] == "not_verified"
    assert report["verification_activity"]["outputs"][0]["locator"] is None
    assert "context" not in report
    rule = {"rule": "r", "version": 1, "interpolated": True}
    with_context = build_report(
        activity_id="a",
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference=None,
        environment=env,
        context={"roi": None, "mask": None, "x_alignment": rule},
    )
    assert with_context["context"]["x_alignment"] == rule
    with pytest.raises(ReportError):
        build_report(
            activity_id="a",
            restoration="bogus",
            eligibility="ready",
            inputs=[],
            reference=None,
            environment=env,
        )


class _Contract:
    """Fake contract for preparation tests."""

    def __init__(self, preconditions_ok: bool = True) -> None:
        self.ok = preconditions_ok


def _ledger_with_activity(opaque: bool = False) -> tuple[Ledger, str]:
    ledger = Ledger()
    src = signal([-2.0, 0.0, 1.0, 4.0])
    state = ledger.observe(SRC_UUID, signal_state_facts(src))
    call = (
        make_call(None, None, {"value": 1.0}, [("source", state)])
        if opaque
        else make_call(
            "sigima.signal.normalize", 1, {"method": "maximum"}, [("source", state)]
        )
    )
    act = ledger.record_activity(
        call=call,
        outputs=[("result", DST_UUID, signal_state_facts(signal([-0.5, 0, 0.25, 1])))],
        environment=collect_environment("desktop", "1.4.0", packages=()),
        edition="desktop",
        origin="ordinary",
        implementation={"package": "x", "version": None, "python_name": "x.f"},
    )
    return ledger, act["activity_id"]


def _prepare(ledger, activity_id, objects, **overrides):
    def resolve(op_id, version):
        if op_id != "sigima.signal.normalize":
            raise IneligibleError("unsupported_operation")
        if version != 1:
            raise IneligibleError("unsupported_contract")
        return _Contract()

    def decode(contract, values):
        if values != {"method": "maximum"}:
            raise IneligibleError("invalid_parameters", "bad")
        return values

    kwargs = {
        "resolve_contract": resolve,
        "find_object": objects.get,
        "observe_object": signal_state_facts,
        "check_preconditions": lambda c, objs: (
            "no_roi" if any(o.roi is not None for o in objs) else None
        ),
        "decode_parameters": decode,
    }
    kwargs.update(overrides)
    return prepare_activity(ledger, activity_id, **kwargs)


def test_prepare_ready() -> None:
    """A qualified activity with an unchanged source is ready."""
    ledger, act_id = _ledger_with_activity()
    plan = _prepare(ledger, act_id, {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0])})
    assert isinstance(plan, Plan)
    assert plan.parameters == {"method": "maximum"}
    assert [role for role, _s, _o in plan.inputs] == ["source"]
    assert plan.call_inputs == (plan.inputs[0][2],)


def test_prepare_applies_the_recorded_context() -> None:
    """The recorded context gives the call inputs, or refuses the replay."""
    ledger, act_id = _ledger_with_activity()
    ledger.activity(act_id)["context"]["x_alignment"] = {"rule": "r", "version": 1}
    source = signal([-2.0, 0.0, 1.0, 4.0])
    seen = []

    def apply_context(contract, objs, context):
        seen.append(context["x_alignment"])
        if context["x_alignment"]["version"] != 1:
            raise IneligibleError("unsupported_context", "unknown rule")
        return [f"aligned {len(objs)}"]

    plan = _prepare(ledger, act_id, {SRC_UUID: source}, apply_context=apply_context)
    assert plan.call_inputs == ("aligned 1",) and plan.inputs[0][2] is source
    assert seen == [{"rule": "r", "version": 1}]
    ledger.activity(act_id)["context"]["x_alignment"]["version"] = 2
    refusal = _prepare(ledger, act_id, {SRC_UUID: source}, apply_context=apply_context)
    assert refusal.eligibility == "unsupported_context"
    assert [i["status"] for i in refusal.input_statuses] == ["unsupported"]


@pytest.mark.parametrize(
    ("objects", "overrides", "code"),
    [
        ({}, {}, "missing_input"),
        ({SRC_UUID: signal([-2.0, 0.0, 1.0, 5.0])}, {}, "input_changed"),
        (
            {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0], roi=ROI([0.0, 0.5]))},
            {},
            "unsupported_context",
        ),
        (
            {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0])},
            {
                "resolve_contract": lambda i, v: (_ for _ in ()).throw(
                    IneligibleError("unsupported_contract")
                )
            },
            "unsupported_contract",
        ),
        (
            {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0])},
            {
                "decode_parameters": lambda c, v: (_ for _ in ()).throw(
                    IneligibleError("invalid_parameters")
                )
            },
            "invalid_parameters",
        ),
    ],
)
def test_prepare_refusals(objects, overrides, code) -> None:
    """Each failure maps to its eligibility code before any computation."""
    ledger, act_id = _ledger_with_activity()
    refusal = _prepare(ledger, act_id, objects, **overrides)
    assert isinstance(refusal, Refusal)
    assert refusal.eligibility == code
    assert refusal.restoration == "replayable"


def test_prepare_altered_and_opaque() -> None:
    """An altered input state and an opaque call are refused."""
    ledger, act_id = _ledger_with_activity()
    state_id = ledger.activity(act_id)["call"]["inputs"][0]["binding"]["state_id"]
    refusal = _prepare(
        ledger,
        act_id,
        {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0])},
        state_status={state_id: "altered"},
    )
    assert refusal.eligibility == "input_changed"
    opaque, opaque_id = _ledger_with_activity(opaque=True)
    refusal = _prepare(opaque, opaque_id, {SRC_UUID: signal([-2.0, 0.0, 1.0, 4.0])})
    assert (refusal.restoration, refusal.eligibility) == (
        "opaque",
        "unsupported_operation",
    )
