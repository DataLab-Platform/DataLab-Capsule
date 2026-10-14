# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for legacy workspaces: processing metadata imported as activities."""

from __future__ import annotations

import io
import json

import h5py
import numpy as np
import pytest

from datalab_capsule.calls import make_call
from datalab_capsule.environment import collect_environment
from datalab_capsule.hdf5 import load_ledger, locate_current_states, save_ledger
from datalab_capsule.integrity import state_facts
from datalab_capsule.ledger import Ledger, LedgerError, validate_ledger
from datalab_capsule.legacy import (
    ANALYSIS_KEY,
    LEGACY_LIMIT,
    PROCESSING_KEY,
    LegacyObject,
    import_legacy,
    legacy_summary,
)
from datalab_capsule.manifest import build_manifest, inspect_manifest, validate_manifest
from datalab_capsule.replay import Refusal, prepare_activity

from .helpers import signal

RAW, A, B, C, E, F, G = (f"00000000-0000-4000-8000-0000000000{i:02d}" for i in range(7))
MISSING = "00000000-0000-4000-8000-0000000000ff"
SHA = "sha256:" + "0" * 64
PARAM = json.dumps(
    {
        "class_module": "sigima.params",
        "class_name": "NormalizeParam",
        "method": "maximum",
        "offset": float("nan"),
    }
)


def _object(uid: str, y, **metadata) -> LegacyObject:
    sig = signal(y)
    return LegacyObject(uid, "signal", dict(metadata), state_facts(sig))


def _workspace() -> list[LegacyObject]:
    """RAW -> A (normalize) ; (A, missing) -> B ; (A, B, RAW) -> C ; analysis on A."""
    return [
        _object(RAW, [1.0, 2.0, 3.0, 4.0]),
        _object(
            A,
            [0.25, 0.5, 0.75, 1.0],
            __processing_parameters={
                "func_name": "normalize",
                "pattern": "1-to-1",
                "param_json": PARAM,
                "source_uuid": RAW,
            },
            __analysis_parameters={
                "func_name": "stats",
                "pattern": "1-to-0",
                "source_uuid": A,
            },
        ),
        _object(
            B,
            [0.0, 0.0, 0.0, 0.0],
            __processing_parameters={
                "func_name": "difference",
                "pattern": "2-to-1",
                "source_uuids": [A, MISSING],
            },
        ),
        _object(
            C,
            [1.0, 1.0, 1.0, 1.0],
            __processing_parameters={
                "func_name": "average",
                "pattern": "n-to-1",
                "source_uuids": [A, B, RAW],
            },
        ),
        _object(
            E,
            [5.0, 5.0, 5.0, 5.0],
            __processing_parameters={
                "func_name": "my_plugin_filter",
                "pattern": "1-to-1",
                "param_json": "{not json",
                "plugin_origin": {"module": "my_plugin"},
            },
        ),
        _object(F, [6.0, 6.0, 6.0, 6.0], __processing_parameters="garbage"),
        LegacyObject(
            G,
            "signal",
            {PROCESSING_KEY: {"func_name": "normalize", "pattern": "weird"}},
            None,
        ),
    ]


def _known(_kind: str, name: str, _origin) -> bool:
    return name in ("normalize", "difference", "average", "stats")


def _import(ledger=None, saved="1.3.0", current="1.3.0"):
    ledger = ledger or Ledger()
    activities = import_legacy(
        ledger,
        _workspace(),
        is_known=_known,
        saved_version=saved,
        current_version=current,
    )
    return ledger, activities


def _inputs(activity) -> list[tuple[str, str]]:
    return [
        (i["role"], i["binding"]["object_uuids"][0]) for i in activity["call"]["inputs"]
    ]


def test_records_become_activities_with_explicit_unknowns() -> None:
    """Each record gives one opaque activity; nothing missing is invented."""
    ledger, activities = _import()
    ledger.validate()
    normalize, stats, diff, average, plugin, garbage, weird = activities
    for activity in activities:
        assert activity["origin"] == "legacy_metadata"
        assert activity["call"]["operation"] is None
        assert activity["call"]["parameters"] is None
        assert activity["limits"] == [LEGACY_LIMIT]
        assert activity["environment_id"] is None and activity["edition"] is None
        assert activity["started_at"] is None and activity["implementation"] is None
    assert _inputs(normalize) == [("source", RAW)]
    assert _inputs(diff) == [("source", A), ("operand", MISSING)]
    assert _inputs(average) == [("sources", A), ("sources", B), ("sources", RAW)]
    # The stored parameter text is kept and read as plain JSON (no import).
    legacy = normalize["legacy"]
    assert legacy["record"]["param_json"] == PARAM
    assert legacy["parameters"] == {
        "class": "sigima.params.NormalizeParam",
        "values": {"method": "maximum", "offset": {"$float": "NaN"}},
    }
    assert legacy["issues"] == [] and legacy["saved_by_version"] == "1.3.0"
    # No historical result is claimed: the state observed at opening is an
    # ordinary observed state, named separately and produced by nothing.
    for activity in activities:
        assert activity["outputs"] == []
    state = ledger.states[legacy["observed_state_id"]]
    assert state["object_uuid"] == A and state["produced_by"] is None
    # Analysis: input is the analysed object, result key unknown, nothing observed.
    assert stats["legacy"]["key"] == ANALYSIS_KEY
    assert "observed_state_id" not in stats["legacy"]
    assert _inputs(stats) == [("source", A)]
    assert stats["legacy"]["issues"] == ["result_not_identified"]
    assert diff["legacy"]["issues"] == ["unavailable_input"]
    assert average["legacy"]["issues"] == []
    assert plugin["legacy"]["issues"] == [
        "missing_ancestry",
        "unsupported_operation",
        "parameters_unreadable",
    ]
    assert plugin["legacy"]["record"]["plugin_origin"] == {"module": "my_plugin"}
    assert "parameters" not in plugin["legacy"]
    observed_f = garbage["legacy"].pop("observed_state_id")
    assert garbage["legacy"] == {
        "key": PROCESSING_KEY,
        "object_uuid": F,
        "record": "garbage",
        "saved_by_version": "1.3.0",
        "issues": ["record_unreadable"],
    }
    assert garbage["call"]["inputs"] == []
    assert ledger.states[observed_f]["object_uuid"] == F
    assert weird["legacy"]["issues"] == [
        "missing_ancestry",
        "pattern_unknown",
        "output_not_observed",
    ]
    assert weird["legacy"]["observed_state_id"] is None


def test_modified_old_result_is_not_attested() -> None:
    """Data changed after the processing, record unchanged: no result is claimed."""
    modified = [
        LegacyObject(o.object_uuid, o.kind, o.metadata, state_facts(signal([9.0] * 4)))
        if o.object_uuid == A
        else o
        for o in _workspace()
    ]
    ledger = Ledger()
    (normalize, *_rest) = import_legacy(
        ledger, modified, is_known=_known, saved_version=None, current_version=None
    )
    # The import cannot know: the record is the same, the observed state shows the
    # current data, and nothing presents it as the result of the record.
    assert normalize["legacy"]["record"]["func_name"] == "normalize"
    assert normalize["outputs"] == []
    observed = ledger.states[normalize["legacy"]["observed_state_id"]]
    assert observed["fingerprint"] == state_facts(signal([9.0] * 4))["fingerprint"]
    assert all(s["produced_by"] is None for s in ledger.states.values())
    manifest = build_manifest(
        ledger, {}, workspace_sha256=SHA, workspace_size=1, date_published="x"
    )
    validate_manifest(manifest)
    entities = {e["@id"]: e for e in manifest["@graph"]}
    action = entities[f"#activity-{normalize['activity_id']}"]
    assert action["result"] == [] and action["dlc:outputs"] == []
    state_ref = f"#state-{observed['state_id']}"
    assert action["dlc:observedState"] == {"@id": state_ref}
    assert "dlc:producedBy" not in entities[state_ref]
    first = inspect_manifest(manifest)["activities"][0]
    assert first["outputs"] == [] and first["observed_state"] == state_ref


@pytest.mark.parametrize(
    "saved, current, expected",
    [
        ("1.3.0", "1.3.0", []),
        ("1.2.0", "1.3.0", ["version_mismatch"]),
        (None, "1.3.0", ["version_unknown"]),
        ("1.2.0", None, []),
    ],
)
def test_version_warnings(saved, current, expected) -> None:
    """The saving version is compared only when both versions are comparable."""
    _ledger, activities = _import(saved=saved, current=current)
    assert activities[0]["legacy"]["issues"] == expected
    assert activities[0]["legacy"]["saved_by_version"] == saved


def test_summary_and_known_objects_are_skipped() -> None:
    """The summary counts issues; objects already in the ledger are not imported."""
    ledger, _activities = _import()
    summary = legacy_summary(ledger)
    assert summary["activities"] == 7
    assert summary["issues"]["unavailable_input"] == 1
    assert summary["issues"]["missing_ancestry"] == 2
    first = summary["items"][0]
    assert first["object_uuid"] == A and first["operation"] == "normalize"
    assert first["inputs"] == [RAW] and first["pattern"] == "1-to-1"
    # A second opening with the same ledger imports nothing new, even for G,
    # which has no state.
    again = import_legacy(
        ledger, _workspace(), is_known=_known, saved_version=None, current_version=None
    )
    assert again == []
    assert legacy_summary(ledger)["activities"] == 7


def test_legacy_activities_are_never_replayed() -> None:
    """Preparation refuses a legacy activity before looking at its inputs."""
    ledger, activities = _import()

    def fail(*_args):
        raise AssertionError("must not be called")

    refusal = prepare_activity(
        ledger,
        activities[0]["activity_id"],
        resolve_contract=fail,
        find_object=fail,
        observe_object=fail,
        check_preconditions=fail,
        decode_parameters=fail,
    )
    assert isinstance(refusal, Refusal)
    assert (refusal.restoration, refusal.eligibility) == (
        "opaque",
        "unsupported_operation",
    )


def test_object_bindings_are_reserved_to_legacy_activities() -> None:
    """Ordinary activities keep state bindings; legacy ones name no operation."""
    ledger, activities = _import()
    data = ledger.to_dict()
    data["activities"][0]["origin"] = "ordinary"
    with pytest.raises(LedgerError):
        validate_ledger(data)
    data = ledger.to_dict()
    data["activities"][0]["call"]["operation"] = {
        "id": "sigima.signal.normalize",
        "contract_version": 1,
    }
    with pytest.raises(LedgerError, match="never names an operation"):
        validate_ledger(data)
    data = ledger.to_dict()
    del data["activities"][0]["legacy"]
    with pytest.raises(LedgerError):
        validate_ledger(data)


def test_legacy_and_ordinary_activities_coexist() -> None:
    """New processing after import records ordinary activities on top."""
    ledger, activities = _import()
    state_a = ledger.latest_state(A)["state_id"]
    ledger.record_activity(
        call=make_call(None, None, {}, [("source", state_a)]),
        outputs=[("result", MISSING, state_facts(signal([1.0, 1.0, 1.0, 1.0])))],
        environment=collect_environment("desktop", "1.3.0", packages=()),
        edition="desktop",
        origin="ordinary",
        implementation={"package": "p", "version": None, "python_name": "p.f"},
    )
    ledger.validate()
    assert (
        ledger.activities[-1]["call"]["inputs"][0]["binding"]["state_id"]
        == activities[0]["legacy"]["observed_state_id"]
    )


def _write_signal(h5file, path: str, y, object_uuid: str) -> None:
    sig = signal(y)
    group = h5file.create_group(path)
    group.attrs["xunit"], group.attrs["yunit"] = sig.xunit, sig.yunit
    group["xydata"] = np.vstack([sig.x, sig.y])
    group.create_group("metadata").attrs["__uuid"] = object_uuid


def test_hdf5_round_trip_and_manifest() -> None:
    """Legacy activities survive the HDF5 block and appear in the manifest."""
    ledger, activities = _import()
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as h5file:
        for index, obj in enumerate(_workspace()[:4]):
            y = [[1.0, 2.0, 3.0, 4.0], [0.25, 0.5, 0.75, 1.0], [0.0] * 4, [1.0] * 4]
            _write_signal(
                h5file, f"/DataLab_Sig/g: G/{index}: S", y[index], obj.object_uuid
            )
        located = locate_current_states(h5file, ledger)
        assert {ledger.states[s]["object_uuid"] for s in located} == {A, B, C}
        save_ledger(h5file, ledger)
        restored, status = load_ledger(h5file)
    assert restored.to_dict() == ledger.to_dict()
    assert {ledger.states[s]["object_uuid"] for s in status} == {E, F}
    manifest = build_manifest(
        ledger, located, workspace_sha256=SHA, workspace_size=1, date_published="x"
    )
    validate_manifest(manifest)
    summary = inspect_manifest(manifest)
    first = summary["activities"][0]
    assert first["origin"] == "legacy_metadata" and first["replayable"] is False
    assert first["name"] == "normalize" and first["instrument"] == ["normalize"]
    assert first["inputs"] == [
        {"role": "source", "position": 0, "target": None, "object_uuid": RAW}
    ]
    assert first["legacy"]["issues"] == [] and first["parameters"] == {}
    assert first["outputs"] == []
    assert first["observed_state"] == (
        f"#state-{activities[0]['legacy']['observed_state_id']}"
    )
