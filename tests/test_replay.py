# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for comparison rules, reports and replay preparation."""

from __future__ import annotations

import numpy as np
import pytest

from datalab_capsule.calls import make_call
from datalab_capsule.compare import (
    ReportError,
    build_report,
    compare_artifacts,
    compare_environments,
    compare_exact,
    compare_rows,
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
    moved = compare_exact(nan_moved, ref)["observed"]
    assert moved["mismatches"] == 2 and moved["max_abs_error"] is None
    float32 = {k: v.astype(np.float32) for k, v in ref.items()}
    assert compare_exact(float32, ref)["verdict"] == "different"
    short = {"x": np.array([0.0]), "y": np.array([1.0])}
    assert compare_exact(short, ref)["observed"]["shape_match"] is False
    zero_ref = {"y": np.array([0.0])}
    observed = compare_exact({"y": np.array([1.0])}, zero_ref)["observed"]
    assert observed["max_rel_error"] is None and observed["max_abs_error"] == 1.0


def test_undefined_errors_are_not_hidden() -> None:
    """A maximum is never computed over the defined errors only."""
    result = compare_exact({"y": np.array([1.0, 2.0])}, {"y": np.array([0.0, 1.0])})
    assert result["verdict"] == "different"
    observed = result["observed"]
    assert observed["mismatches"] == 2 and observed["max_abs_error"] == 1.0
    # The relative error is undefined for the zero reference: no misleading 1.
    assert observed["max_rel_error"] is None
    assert (observed["undefined_abs_errors"], observed["undefined_rel_errors"]) == (
        0,
        1,
    )
    moved = compare_exact(
        {"y": np.array([np.nan, 1.0, 3.0])}, {"y": np.array([1.0, np.nan, 2.0])}
    )["observed"]
    assert moved["mismatches"] == 3
    assert moved["max_abs_error"] is None and moved["max_rel_error"] is None
    assert (moved["undefined_abs_errors"], moved["undefined_rel_errors"]) == (2, 2)
    infinite = compare_exact({"y": np.array([np.inf])}, {"y": np.array([1.0])})
    assert infinite["observed"]["max_abs_error"] is None
    defined = compare_exact({"y": np.array([1.0, 3.0])}, {"y": np.array([1.0, 2.0])})
    assert defined["observed"]["max_rel_error"] == 0.5
    assert defined["observed"]["undefined_rel_errors"] == 0
    same = compare_exact({"y": np.array([0.0])}, {"y": np.array([0.0])})["observed"]
    assert (same["max_abs_error"], same["max_rel_error"]) == (0.0, 0.0)


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


@pytest.mark.parametrize(
    ("recorded", "live", "ready"),
    [
        (None, None, True),
        (ROI([0.0, 0.5]), ROI([0.0, 0.5]), True),
        (None, ROI([0.0, 0.5]), False),
        (ROI([0.0, 0.5]), None, False),
        (ROI([0.0, 0.5]), ROI([0.0, 0.75]), False),
        (ROI([0.0, 0.5]), ROI([0.0, 0.5], kind="RectangularROI"), False),
    ],
)
def test_prepare_checks_the_input_roi(recorded, live, ready) -> None:
    """An input whose ROI was added, removed or changed is refused."""
    ledger = Ledger()
    values = [-2.0, 0.0, 1.0, 4.0]
    state = ledger.observe(SRC_UUID, signal_state_facts(signal(values, roi=recorded)))
    act = ledger.record_activity(
        call=make_call(
            "sigima.signal.normalize", 1, {"method": "maximum"}, [("source", state)]
        ),
        outputs=[("result", DST_UUID, signal_state_facts(signal([-0.5, 0, 0.25, 1])))],
        environment=collect_environment("desktop", "1.4.0", packages=()),
        edition="desktop",
        origin="ordinary",
    )
    result = _prepare(
        ledger,
        act["activity_id"],
        {SRC_UUID: signal(values, roi=live)},
        check_preconditions=lambda c, objs: None,
    )
    if ready:
        assert isinstance(result, Plan)
    else:
        assert isinstance(result, Refusal)
        assert result.eligibility == "input_changed"
        assert "region of interest" in result.reason
        assert [i["status"] for i in result.input_statuses] == ["changed"]


def test_compare_complex_keeps_the_imaginary_part() -> None:
    """Complex rows are compared on both parts; errors use the modulus."""
    ref = {"y": np.array([1 + 1j, 2 - 1j, complex(np.nan, 1.0)])}
    assert compare_exact(ref, ref)["verdict"] == "exact"
    conj = {"y": np.array([1 - 1j, 2 - 1j, complex(np.nan, 1.0)])}
    result = compare_rows(conj, ref)
    assert result["verdict"] == "different"
    assert result["observed"]["mismatches"] == 1
    assert result["observed"]["max_abs_error"] == 2.0
    moved = {"y": np.array([1 + 1j, 2 - 1j, complex(1.0, np.nan)])}
    observed = compare_rows(moved, ref)["observed"]
    assert observed["mismatches"] == 1 and observed["max_abs_error"] is None
    real = {"y": ref["y"].real}
    assert compare_rows(real, ref)["verdict"] == "different"


TOLERANCE = {
    "rule": "tolerance",
    "outputs": {"result": {"atol": 1e-12, "rtol": 1e-9}},
    "same_environment": "exact",
    "justification": "FFT rounding differs across NumPy versions",
}


def test_compare_rows_with_a_declared_tolerance() -> None:
    """A declared tolerance gives within_tolerance across environments only."""
    ref = {"x": np.array([0.0, 1.0]), "y": np.array([1.0, 1j])}
    close = {"x": ref["x"], "y": ref["y"] + 1e-13}
    result = compare_rows(close, ref, TOLERANCE, environment="different")
    assert result["verdict"] == "within_tolerance"
    assert result["rule"]["kind"] == "tolerance"
    assert (result["rule"]["atol"], result["rule"]["rtol"]) == (1e-12, 1e-9)
    assert result["rule"]["declared"] == TOLERANCE
    assert result["observed"]["out_of_tolerance"] == 0
    assert result["observed"]["mismatches"] == 2
    same = compare_rows(close, ref, TOLERANCE, environment="same")
    assert same["verdict"] == "different" and same["rule"]["kind"] == "exact"
    assert compare_rows(ref, ref, TOLERANCE, environment="different")["verdict"] == (
        "exact"
    )
    far = {"x": ref["x"], "y": ref["y"] + 1e-6}
    result = compare_rows(far, ref, TOLERANCE, environment="unknown")
    assert result["verdict"] == "different"
    assert result["observed"]["out_of_tolerance"] == 2
    nan = {"x": ref["x"], "y": np.array([np.nan, 1j])}
    assert compare_rows(nan, ref, TOLERANCE)["verdict"] == "different"
    other_output = compare_rows(close, ref, TOLERANCE, output="mask")
    assert other_output["rule"]["kind"] == "exact"
    assert other_output["verdict"] == "different"
    assert compare_rows(ref, ref, {"rule": "exact"})["rule"] == {"kind": "exact"}
    for bad in ({"rule": "close"}, {"rule": "tolerance", "outputs": None}):
        with pytest.raises(ValueError):
            compare_rows(ref, ref, bad)
    env = compare_environments(None, None, None, None)
    report = build_report(
        activity_id="a",
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference={"state_id": "s", "status": "available"},
        environment=env,
        comparison=compare_rows(close, ref, TOLERANCE, environment=env["match"]),
        candidate_state_ids=[("result", "c")],
    )
    assert report["verdict"] == "within_tolerance"
    assert report["rule"]["declared"]["justification"] == TOLERANCE["justification"]


TABLE = {
    "schema": 1,
    "title": "Statistics",
    "kind": "statistics",
    "names": ["mean", "std"],
    "data": [[1.0, 0.5], [2.0, float("nan")]],
    "roi_indices": [0, 1],
    "func_name": "stats",
    "attrs": {"param_json": "{}"},
}


def test_compare_analysis_results() -> None:
    """Analysis results: values under the rule, structure exactly."""
    other_attrs = {**TABLE, "title": "Stats", "attrs": {}, "func_name": None}
    result = compare_artifacts("table", other_attrs, TABLE)
    assert result["verdict"] == "exact" and result["observed"]["structure_match"]
    close = {**TABLE, "data": [[1.0 + 1e-13, 0.5], [2.0, float("nan")]]}
    assert compare_artifacts("table", close, TABLE)["verdict"] == "different"
    tolerant = compare_artifacts(
        "table", close, TABLE, TOLERANCE, environment="different"
    )
    assert tolerant["verdict"] == "within_tolerance"
    for changed in (
        {**TABLE, "names": ["mean", "sigma"]},
        {**TABLE, "roi_indices": [0, 2]},
        {**TABLE, "data": [[1.0, 0.5]], "roi_indices": [0, 1]},
    ):
        assert compare_artifacts("table", changed, TABLE)["verdict"] == "different"
    text = {**TABLE, "data": [["a", 1.0]]}
    assert compare_artifacts("table", text, text)["verdict"] == "exact"
    assert (
        compare_artifacts("table", {**text, "data": [["b", 1.0]]}, text)["observed"][
            "structure_match"
        ]
        is False
    )
    geometry = {
        "schema": 1,
        "title": "FWHM",
        "kind": "segment",
        "coords": np.array([[0.0, 1.0, 2.0, 1.0]]),
        "roi_indices": None,
        "func_name": "fwhm",
        "attrs": {},
    }
    as_lists = {**geometry, "coords": [[0.0, 1.0, 2.0, 1.0]]}
    assert compare_artifacts("geometry", as_lists, geometry)["verdict"] == "exact"
    point = {**geometry, "kind": "point"}
    assert compare_artifacts("geometry", point, geometry)["verdict"] == "different"


PULSES = {
    "schema": 1,
    "title": "Pulse features",
    "kind": "pulse_features",
    "names": ["signal_shape", "amplitude", "rise_time"],
    "data": [["step", 1.0, None], ["square", 2.0, float("nan")]],
    "roi_indices": [0, 1],
    "func_name": "extract_pulse_features",
    "attrs": {},
}


def test_compare_tables_mixing_texts_and_numbers() -> None:
    """Numeric columns of a mixed table are compared under the rule."""
    same = {**PULSES, "data": [["step", 1.0, None], ["square", 2.0, float("nan")]]}
    assert compare_artifacts("table", same, PULSES)["verdict"] == "exact"
    close = {
        **PULSES,
        "data": [["step", 1.0 + 1e-13, None], ["square", 2.0, float("nan")]],
    }
    assert compare_artifacts("table", close, PULSES)["verdict"] == "different"
    tolerant = compare_artifacts(
        "table", close, PULSES, TOLERANCE, environment="different"
    )
    assert tolerant["verdict"] == "within_tolerance"
    assert tolerant["observed"]["structure_match"] is True
    for data in (
        [["square", 1.0, None], ["square", 2.0, float("nan")]],  # another text
        [["step", 1.0, float("nan")], ["square", 2.0, float("nan")]],  # not empty
        [["step", 1.0, None], ["square", 2.0, 0.5]],  # another number
    ):
        changed = {**PULSES, "data": data}
        assert (
            compare_artifacts(
                "table", changed, PULSES, TOLERANCE, environment="different"
            )["verdict"]
            == "different"
        )


def test_report_compares_every_output() -> None:
    """The verdict of a report is the most severe verdict of its outputs."""
    env = compare_environments(None, None, None, None)
    exact = compare_rows({"y": np.ones(2)}, {"y": np.ones(2)})

    def output(role: str, verdict: str, compared: bool = True) -> dict:
        return {
            "role": role,
            "recorded": "artifact",
            "artifact": {"object_uuid": "u", "key": "k"},
            "status": "available" if compared else "replaced",
            "verdict": verdict,
            "rule": exact["rule"] if compared else None,
            "observed": exact["observed"] if compared else None,
            "detail": None,
        }

    common = {
        "activity_id": "a",
        "restoration": "replayable",
        "eligibility": "ready",
        "inputs": [],
        "reference": None,
        "environment": env,
        "candidate_state_ids": [],
    }
    report = build_report(outputs=[output("result", "exact")], **common)
    assert report["verdict"] == "exact" and report["rule"] == {"kind": "exact"}
    assert report["outputs"][0]["artifact"] == {"object_uuid": "u", "key": "k"}
    replaced = output("mask", "not_verified", compared=False)
    report = build_report(outputs=[output("result", "exact"), replaced], **common)
    assert report["verdict"] == "not_verified"
    different = output("mask", "different")
    report = build_report(outputs=[replaced, different], **common)
    assert report["verdict"] == "different"
    none = {
        "role": "result",
        "recorded": "none",
        "status": "available",
        "verdict": "exact",
        "rule": None,
        "observed": None,
        "detail": None,
    }
    report = build_report(outputs=[none], **common)
    assert (report["verdict"], report["rule"], report["observed"]) == (
        "exact",
        None,
        None,
    )
