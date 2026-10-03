# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Generate the expressiveness fixtures (ledgers and reports written by hand).

Each fixture describes a processing shape that applications will capture later:
multi-input roles, ordered fan-in, several outputs, analysis artifacts, fan-out
with a missing intermediate state, an opaque step without encodable parameters,
parameter value types, and a ``within_tolerance`` report. No application is
involved. Run ``python generate.py`` to rewrite the JSON files.
"""

from __future__ import annotations

import json
from pathlib import Path

from datalab_capsule.environment import environment_id

HERE = Path(__file__).parent

ENVIRONMENT = {
    "edition": "desktop",
    "edition_version": "1.4.0",
    "edition_revision": None,
    "python": {"implementation": "CPython", "version": "3.12.10"},
    "platform": "win32",
    "machine": "AMD64",
    "pyodide": None,
    "packages": {
        "sigima": "1.3.0",
        "guidata": "3.16.0",
        "numpy": "2.3.0",
        "scipy": "1.16.0",
        "h5py": "3.14.0",
    },
}
ENV_ID = environment_id(ENVIRONMENT)


def uid(number: int) -> str:
    """Return a readable, schema-valid UUID."""
    return f"00000000-0000-4000-8000-{number:012d}"


def state(state_id: int, obj: int, produced_by: int | None, seq: int) -> dict:
    """Return a signal state."""
    return {
        "state_id": uid(state_id),
        "object_uuid": uid(obj),
        "kind": "signal",
        "fingerprint": {
            "scheme": "datalab-signal-v1",
            "value": "sha256:" + f"{state_id:064x}",
        },
        "dtype": "float64",
        "length": 4,
        "rows": ["x", "y"],
        "units": {"x": "s", "y": ""},
        "limits": [],
        "produced_by": None if produced_by is None else uid(produced_by),
        "sequence": seq,
    }


def activity(act: int, operation, parameters, inputs, outputs, **extra) -> dict:
    """Return a completed activity."""
    data = {
        "activity_id": uid(act),
        "command_id": uid(act + 500),
        "call": {
            "operation": operation,
            "parameters": parameters,
            "inputs": [
                {"role": role, "binding": {"state_id": uid(s)}} for role, s in inputs
            ],
        },
        "implementation": extra.pop("implementation", None),
        "outputs": outputs,
        "context": {"roi": None, "mask": None, "x_alignment": None},
        "limits": extra.pop("limits", []),
        "environment_id": ENV_ID,
        "edition": "desktop",
        "origin": "ordinary",
        "status": "completed",
        "started_at": "2026-10-03T08:00:00.000Z",
        "finished_at": "2026-10-03T08:00:00.010Z",
    }
    data.update(extra)
    return data


def ledger(states: list[dict], activities: list[dict]) -> dict:
    """Return a ledger."""
    return {
        "schema": "datalab-provenance-ledger",
        "schema_version": 1,
        "workspace_id": uid(999),
        "environments": {ENV_ID: ENVIRONMENT},
        "states": {s["state_id"]: s for s in states},
        "activities": activities,
    }


def op(name: str) -> dict:
    """Return an operation reference."""
    return {"id": f"sigima.signal.{name}", "contract_version": 1}


def out(role: str, s: int) -> dict:
    """Return a state output."""
    return {"role": role, "state_id": uid(s)}


def locate(*states: int) -> dict:
    """Return locators for the given states."""
    return {
        uid(s): {"kind": "signal", "path": f"/DataLab_Sig/g001: Group/s{s:03d}: obj"}
        for s in states
    }


def build() -> dict[str, dict]:
    """Return every fixture, by file name."""
    fixtures = {}
    fixtures["difference"] = {
        "ledger": ledger(
            [state(1, 101, None, 0), state(2, 102, None, 1), state(3, 103, 201, 2)],
            [
                activity(
                    201,
                    op("difference"),
                    {},
                    [("source", 1), ("operand", 2)],
                    [out("result", 3)],
                )
            ],
        ),
        "locators": locate(1, 2, 3),
    }
    fixtures["average_fan_in"] = {
        "ledger": ledger(
            [
                state(1, 101, None, 0),
                state(2, 102, None, 1),
                state(3, 103, None, 2),
                state(4, 104, 201, 3),
            ],
            [
                activity(
                    201,
                    op("average"),
                    {},
                    [("sources", 3), ("sources", 1), ("sources", 2)],
                    [out("result", 4)],
                )
            ],
        ),
        "locators": locate(1, 2, 3, 4),
    }
    fixtures["one_to_n"] = {
        "ledger": ledger(
            [state(1, 101, None, 0), state(2, 102, 201, 1), state(3, 103, 201, 2)],
            [
                activity(
                    201,
                    op("extract_rois"),
                    {"roi_indices": [1, 0]},
                    [("source", 1)],
                    [out("results", 3), out("results", 2)],
                )
            ],
        ),
        "locators": locate(1, 2, 3),
    }
    fixtures["analysis_artifact"] = {
        "ledger": ledger(
            [state(1, 101, None, 0)],
            [
                activity(
                    201,
                    op("fwhm"),
                    {"method": "zero-crossing"},
                    [("source", 1)],
                    [
                        {
                            "role": "result",
                            "artifact": {
                                "kind": "table",
                                "object_uuid": uid(101),
                                "key": "__result_fwhm",
                            },
                        }
                    ],
                )
            ],
        ),
        "locators": locate(1),
    }
    fixtures["fan_out_missing_intermediate"] = {
        "ledger": ledger(
            [
                state(1, 101, None, 0),
                state(2, 102, 201, 1),
                state(3, 103, 202, 2),
                state(4, 104, 203, 3),
            ],
            [
                activity(
                    201,
                    None,
                    {"value": 1.0},
                    [("source", 1)],
                    [out("result", 2)],
                    implementation={
                        "package": "sigima",
                        "version": "1.3.0",
                        "python_name": (
                            "sigima.proc.signal.arithmetic.addition_constant"
                        ),
                    },
                ),
                activity(
                    202,
                    op("normalize"),
                    {"method": "maximum"},
                    [("source", 2)],
                    [out("result", 3)],
                ),
                activity(
                    203,
                    op("normalize"),
                    {"method": "amplitude"},
                    [("source", 1)],
                    [out("result", 4)],
                ),
            ],
        ),
        # The intermediate state 2 was deleted before saving: no locator.
        "locators": locate(1, 3, 4),
    }
    fixtures["opaque_not_encoded"] = {
        "ledger": ledger(
            [state(1, 101, None, 0), state(2, 102, 201, 1)],
            [
                activity(
                    201,
                    None,
                    None,
                    [("source", 1)],
                    [out("result", 2)],
                    implementation={
                        "package": "example-plugin",
                        "version": None,
                        "python_name": "example_plugin.filters.apply_kernel",
                    },
                    limits=["parameters_not_encoded"],
                )
            ],
        ),
        "locators": locate(1, 2),
    }
    fixtures["parameter_types"] = {
        "ledger": ledger(
            [state(1, 101, None, 0), state(2, 102, 201, 1)],
            [
                activity(
                    201,
                    op("clip"),
                    {
                        "count": 3,
                        "ratio": 0.5,
                        "upper": {"$float": "Infinity"},
                        "lower": {"$float": "-Infinity"},
                        "fill": {"$float": "NaN"},
                        "label": None,
                    },
                    [("source", 1)],
                    [out("result", 2)],
                )
            ],
        ),
        "locators": locate(1, 2),
    }
    fixtures["report_within_tolerance"] = {
        "report": {
            "report": "datalab-verification-report",
            "report_version": 1,
            "activity_id": uid(201),
            "restoration": "replayable",
            "eligibility": "ready",
            "reason": None,
            "inputs": [{"role": "source", "state_id": uid(1), "status": "available"}],
            "reference": {"state_id": uid(2), "status": "available"},
            "environment": {
                "recorded": ENV_ID,
                "current": "sha256:" + "f" * 64,
                "match": "different",
                "differences": {"numpy": ["2.3.0", "1.26.4"]},
            },
            "verdict": "within_tolerance",
            "rule": {"kind": "tolerance", "rtol": 1e-12, "atol": 0.0},
            "observed": {
                "mismatches": 2,
                "max_abs_error": 2.220446049250313e-16,
                "max_rel_error": 4.440892098500626e-16,
                "dtype_match": True,
                "shape_match": True,
            },
            "verification_activity": {
                "origin": "verification",
                "outputs": [{"role": "result", "state_id": uid(900), "locator": None}],
            },
        }
    }
    return fixtures


def render(fixture: dict) -> str:
    """Return the JSON text of a fixture."""
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """Write every fixture."""
    for name, fixture in build().items():
        (HERE / f"{name}.json").write_text(render(fixture), encoding="utf-8")


if __name__ == "__main__":
    main()
