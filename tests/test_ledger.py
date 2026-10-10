# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for operation calls and the ledger model."""

from __future__ import annotations

import copy
import json

import pytest

from datalab_capsule.calls import (
    CallError,
    ReservedBindingError,
    make_call,
    validate_call,
)
from datalab_capsule.environment import collect_environment
from datalab_capsule.integrity import signal_state_facts
from datalab_capsule.ledger import Ledger, LedgerError

from .helpers import ROI, signal


def _environment() -> dict:
    return collect_environment("desktop", "1.4.0", packages=("numpy",))


def _chain() -> tuple[Ledger, dict[str, str]]:
    """Return a two-step ledger: S0 -> (opaque +1) -> S1 -> normalize -> S2."""
    ledger = Ledger()
    s0 = signal([-2.0, 0.0, 1.0, 4.0])
    s1 = signal([-1.0, 1.0, 2.0, 5.0])
    s2 = signal([-0.2, 0.2, 0.4, 1.0])
    ids = {
        "S0": "00000000-0000-4000-8000-000000000001",
        "S1": "00000000-0000-4000-8000-000000000002",
        "S2": "00000000-0000-4000-8000-000000000003",
    }
    st0 = ledger.observe(ids["S0"], signal_state_facts(s0))
    act1 = ledger.record_activity(
        call=make_call(None, None, {"value": 1.0}, [("source", st0)]),
        outputs=[("result", ids["S1"], signal_state_facts(s1))],
        environment=_environment(),
        edition="desktop",
        origin="ordinary",
        implementation={
            "package": "sigima",
            "version": "1.3.0",
            "python_name": "sigima.proc.signal.addition_constant",
        },
    )
    st1 = ledger.observe(ids["S1"], signal_state_facts(s1))
    assert st1 == act1["outputs"][0]["state_id"]
    ledger.record_activity(
        call=make_call(
            "sigima.signal.normalize", 1, {"method": "maximum"}, [("source", st1)]
        ),
        outputs=[("result", ids["S2"], signal_state_facts(s2))],
        environment=_environment(),
        edition="desktop",
        origin="ordinary",
    )
    return ledger, ids


def test_make_call_and_reserved_bindings() -> None:
    """State bindings are accepted; reserved kinds are refused explicitly."""
    call = make_call(
        "sigima.signal.normalize", 1, {"method": "maximum"}, [("source", "s")]
    )
    validate_call(call)
    reserved = copy.deepcopy(call)
    reserved["inputs"][0]["binding"] = {"object_uuids": ["a"]}
    with pytest.raises(ReservedBindingError):
        validate_call(reserved)
    reserved["inputs"][0]["binding"] = {"step": "n1", "role": "result"}
    with pytest.raises(ReservedBindingError):
        validate_call(reserved)
    with pytest.raises(CallError):
        make_call("Bad ID", 1, {}, [])
    with pytest.raises(CallError):
        make_call("a.b", 0, {}, [])
    opaque = make_call(None, None, None, [("source", "s")])
    assert opaque["operation"] is None and opaque["parameters"] is None


def test_chain_round_trip_and_state_reuse() -> None:
    """A chain round-trips through JSON; unchanged objects reuse their state."""
    ledger, ids = _chain()
    ledger.validate()
    assert len(ledger.states) == 3 and len(ledger.activities) == 2
    first, second = ledger.activities
    assert first["call"]["operation"] is None
    assert (
        second["call"]["inputs"][0]["binding"]["state_id"]
        == (first["outputs"][0]["state_id"])
    )
    text = ledger.to_json()
    restored = Ledger.from_json(text)
    assert restored.to_json() == text
    assert json.loads(text) == restored.to_dict()
    s0_state = restored.latest_state(ids["S0"])
    assert restored.observe(ids["S0"], signal_state_facts(signal([-2.0, 0, 1, 4])))
    assert restored.latest_state(ids["S0"]) == s0_state
    changed = restored.observe(ids["S0"], signal_state_facts(signal([9.0, 0, 1, 4])))
    assert changed != s0_state["state_id"]
    assert restored.latest_state(ids["S0"])["state_id"] == changed


def test_environment_deduplicated() -> None:
    """Identical environments share one entry."""
    ledger, _ids = _chain()
    assert len(ledger.environments) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["activities"][1]["call"]["inputs"][0]["binding"].update(
            state_id="00000000-0000-4000-8000-0000000000ff"
        ),
        lambda d: d["activities"][0].update(implementation=None),
        lambda d: d["activities"][0].update(environment_id="sha256:" + "0" * 64),
        lambda d: d["activities"].append(copy.deepcopy(d["activities"][0])),
        lambda d: next(iter(d["states"].values())).update(
            produced_by="00000000-0000-4000-8000-0000000000aa"
        ),
        lambda d: d.update(schema_version=2),
        lambda d: d["activities"][0]["call"]["inputs"][0].update(
            binding={"object_uuids": ["00000000-0000-4000-8000-000000000001"]}
        ),
        lambda d: d["environments"][next(iter(d["environments"]))].update(
            machine="other"
        ),
    ],
)
def test_broken_ledgers_are_refused(mutate) -> None:
    """Broken references, opaque calls without implementation and more fail."""
    ledger, _ids = _chain()
    data = ledger.to_dict()
    mutate(data)
    with pytest.raises(LedgerError):
        Ledger.from_dict(data)


def test_record_activity_requires_observed_inputs() -> None:
    """Input states must exist; opaque calls need an implementation."""
    ledger = Ledger()
    facts = signal_state_facts(signal([1.0, 2.0, 3.0, 4.0]))
    with pytest.raises(LedgerError):
        ledger.record_activity(
            call=make_call("a.b", 1, {}, [("source", "missing")]),
            outputs=[],
            environment=_environment(),
            edition="desktop",
            origin="ordinary",
        )
    state_id = ledger.observe("00000000-0000-4000-8000-000000000001", facts)
    with pytest.raises(LedgerError):
        ledger.record_activity(
            call=make_call(None, None, {}, [("source", state_id)]),
            outputs=[],
            environment=_environment(),
            edition="desktop",
            origin="ordinary",
        )
    with pytest.raises(LedgerError):
        ledger.record_activity(
            call=make_call("a.b", 1, {}, [("source", state_id)]),
            outputs=[],
            environment=_environment(),
            edition="desktop",
            origin="verification",
        )


def test_roi_is_part_of_the_state() -> None:
    """Same data with another ROI is a new state; the same ROI reuses it."""
    ledger = Ledger()
    uid = "00000000-0000-4000-8000-000000000001"
    plain = ledger.observe(uid, signal_state_facts(signal([1.0, 2.0, 3.0, 4.0])))
    with_roi = signal([1.0, 2.0, 3.0, 4.0], roi=ROI([0.0, 0.5]))
    first = ledger.observe(uid, signal_state_facts(with_roi))
    second = ledger.observe(uid, signal_state_facts(with_roi))
    other = ledger.observe(
        uid, signal_state_facts(signal([1.0, 2.0, 3.0, 4.0], roi=ROI([0.0, 0.25])))
    )
    assert first == second and len({plain, first, other}) == 3
    state = ledger.states[first]
    assert state["fingerprint"] == ledger.states[plain]["fingerprint"]
    assert state["limits"] == []
    assert state["roi"]["definition"]["single_rois"][0]["coords"] == [0.0, 0.5]
    ledger.validate()


def test_artifact_outputs() -> None:
    """Analysis results are recorded as artifacts after the state outputs."""
    ledger = Ledger()
    uid = "00000000-0000-4000-8000-000000000001"
    state_id = ledger.observe(uid, signal_state_facts(signal([1.0, 2.0])))
    activity = ledger.record_activity(
        call=make_call(None, None, {}, [("source", state_id)]),
        outputs=[],
        artifacts=[("result", "geometry", uid, "_geometry_fwhm")],
        environment=_environment(),
        edition="desktop",
        origin="ordinary",
        implementation={"package": "p", "version": None, "python_name": "p.fwhm"},
    )
    assert activity["outputs"] == [
        {
            "role": "result",
            "artifact": {
                "kind": "geometry",
                "object_uuid": uid,
                "key": "_geometry_fwhm",
            },
        }
    ]
    ledger.validate()
