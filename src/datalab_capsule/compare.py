# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Comparison rules and verification reports.

The ``exact`` rule requires the same rows, dtype and shape, equal values and NaN
at the same positions. A missing reference gives the verdict ``not_verified``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datalab_capsule._schema import schema_errors
from datalab_capsule.integrity import ProvenanceError

__all__ = [
    "ELIGIBILITY_CODES",
    "RESTORATION_STATUSES",
    "VERDICTS",
    "ReportError",
    "build_report",
    "compare_environments",
    "compare_exact",
    "validate_report",
]

RESTORATION_STATUSES = ("included", "replayable", "opaque", "unavailable")
ELIGIBILITY_CODES = (
    "ready",
    "missing_input",
    "input_changed",
    "unsupported_operation",
    "unsupported_contract",
    "unsupported_context",
    "invalid_parameters",
)
VERDICTS = ("exact", "within_tolerance", "different", "not_verified")


class ReportError(ProvenanceError, ValueError):
    """Raised when a verification report is invalid."""


def compare_exact(
    candidate: Mapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare candidate rows with reference rows under the ``exact`` rule.

    Args:
        candidate: Row name -> array (e.g. ``{"x": ..., "y": ...}``).
        reference: Row name -> array.

    Returns:
        ``{"verdict", "rule", "observed"}`` where ``observed`` holds the mismatch
        count and the maximum absolute and relative errors (None when undefined).
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    rule = {"kind": "exact"}
    same_rows = list(candidate) == list(reference)
    dtype_match = same_rows and all(
        np.asarray(candidate[k]).dtype == np.asarray(reference[k]).dtype
        for k in reference
    )
    shape_match = same_rows and all(
        np.asarray(candidate[k]).shape == np.asarray(reference[k]).shape
        for k in reference
    )
    if not (same_rows and shape_match):
        observed = {
            "mismatches": max(
                (np.asarray(v).size for v in reference.values()), default=0
            ),
            "max_abs_error": None,
            "max_rel_error": None,
            "dtype_match": dtype_match,
            "shape_match": shape_match,
        }
        return {"verdict": "different", "rule": rule, "observed": observed}
    mismatches = 0
    max_abs: float | None = None
    max_rel: float | None = None
    for key in reference:
        cand = np.asarray(candidate[key])
        ref = np.asarray(reference[key])
        both_nan = np.isnan(cand) & np.isnan(ref) if cand.dtype.kind in "fc" else False
        differ = ~((cand == ref) | both_nan)
        count = int(np.count_nonzero(differ))
        mismatches += count
        if count:
            with np.errstate(all="ignore"):
                abs_err = np.abs(cand[differ].astype(float) - ref[differ].astype(float))
                rel_err = abs_err / np.abs(ref[differ].astype(float))
            # NaN placed differently counts as a mismatch with no defined error.
            finite_abs = abs_err[np.isfinite(abs_err)]
            if finite_abs.size:
                max_abs = max(max_abs or 0.0, float(finite_abs.max()))
            finite_rel = rel_err[np.isfinite(rel_err)]
            if finite_rel.size:
                max_rel = max(max_rel or 0.0, float(finite_rel.max()))
    if mismatches == 0:
        max_abs, max_rel = 0.0, 0.0
    observed = {
        "mismatches": mismatches,
        "max_abs_error": max_abs,
        "max_rel_error": max_rel,
        "dtype_match": dtype_match,
        "shape_match": shape_match,
    }
    verdict = "exact" if mismatches == 0 and dtype_match else "different"
    return {"verdict": verdict, "rule": rule, "observed": observed}


def _flatten(record: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            # Package versions are reported by package name, as in the example.
            sub_prefix = "" if key == "packages" else f"{name}."
            flat.update(_flatten(value, sub_prefix))
        else:
            flat[name] = value
    return flat


def compare_environments(
    recorded_id: str | None,
    recorded: Mapping[str, Any] | None,
    current_id: str | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe how the current environment differs from the recorded one.

    The match is informative only and never blocks a replay.
    """
    if recorded is None or current is None:
        return {
            "recorded": recorded_id,
            "current": current_id,
            "match": "unknown",
            "differences": {},
        }
    flat_rec, flat_cur = _flatten(recorded), _flatten(current)
    differences = {
        key: [flat_rec.get(key), flat_cur.get(key)]
        for key in sorted(set(flat_rec) | set(flat_cur))
        if flat_rec.get(key) != flat_cur.get(key)
    }
    return {
        "recorded": recorded_id,
        "current": current_id,
        "match": "same" if not differences else "different",
        "differences": differences,
    }


def build_report(
    *,
    activity_id: str,
    restoration: str,
    eligibility: str,
    inputs: list[dict[str, Any]],
    reference: dict[str, Any] | None,
    environment: dict[str, Any],
    comparison: dict[str, Any] | None = None,
    candidate_state_ids: list[tuple[str, str]] | None = None,
    reason: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and validate a verification report.

    Args:
        activity_id: Verified activity.
        restoration: ``included``, ``replayable``, ``opaque`` or ``unavailable``.
        eligibility: Eligibility code.
        inputs: ``{"role", "state_id", "status"}`` items.
        reference: ``{"state_id", "status"}`` or None.
        environment: Result of :func:`compare_environments`.
        comparison: Result of a comparison rule, or None when not compared.
        candidate_state_ids: ``(role, state_id)`` of the candidate outputs, when a
         verification run took place.
        reason: Optional human-readable detail.
        context: Recorded execution context of the activity (e.g. its X-alignment
         rule), shown as is; omitted when None.

    Returns:
        The report.

    Raises:
        ReportError: If the report is invalid.
    """
    verified = comparison is not None and (
        reference is not None and reference["status"] == "available"
    )
    report = {
        "report": "datalab-verification-report",
        "report_version": 1,
        "activity_id": activity_id,
        "restoration": restoration,
        "eligibility": eligibility,
        "reason": reason,
        "inputs": inputs,
        "reference": reference,
        "environment": environment,
        "verdict": comparison["verdict"] if verified else "not_verified",
        "rule": comparison["rule"] if verified else None,
        "observed": comparison["observed"] if verified else None,
        "verification_activity": None
        if candidate_state_ids is None
        else {
            "origin": "verification",
            "outputs": [
                {"role": role, "state_id": state_id, "locator": None}
                for role, state_id in candidate_state_ids
            ],
        },
    }
    if context is not None:
        report["context"] = context
    validate_report(report)
    return report


def validate_report(report: Any) -> None:
    """Validate a verification report against its JSON Schema.

    Raises:
        ReportError: If the report is invalid.
    """
    errors = schema_errors("report-1", report)
    if errors:
        raise ReportError("Invalid report: " + "; ".join(errors[:5]))
