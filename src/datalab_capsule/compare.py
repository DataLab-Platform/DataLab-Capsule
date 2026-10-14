# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Comparison rules and verification reports.

The ``exact`` rule requires the same rows, dtype and shape, equal values and NaN
at the same positions (for complex values, in both parts). A tolerance rule,
declared by the qualification of an operation contract, also accepts values
within an absolute and relative tolerance (verdict ``within_tolerance``). A
missing reference gives the verdict ``not_verified``.
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
    "artifact_content",
    "build_report",
    "compare_artifacts",
    "compare_environments",
    "compare_exact",
    "compare_rows",
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
#: Verdicts by increasing severity: the verdict of a report is the most severe
#: verdict of its outputs.
_SEVERITY = ("exact", "within_tolerance", "not_verified", "different")


class ReportError(ProvenanceError, ValueError):
    """Raised when a verification report is invalid."""


def _equal(cand: Any, ref: Any) -> Any:
    """Return the element-wise equality mask, NaN at the same positions equal.

    Complex values are equal when both their real and imaginary parts are equal
    or NaN at the same positions.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    def same(a: Any, b: Any) -> Any:
        if a.dtype.kind in "fc" and b.dtype.kind in "fc":
            return (a == b) | (np.isnan(a) & np.isnan(b))
        return a == b

    if cand.dtype.kind == "c" or ref.dtype.kind == "c":
        cand_c, ref_c = cand.astype(complex), ref.astype(complex)
        return same(cand_c.real, ref_c.real) & same(cand_c.imag, ref_c.imag)
    return same(cand, ref)


def _errors(cand: Any, ref: Any) -> tuple[Any, Any, Any]:
    """Return absolute errors, relative errors and reference magnitudes.

    Errors are computed on the modulus of the difference (complex values keep
    their imaginary part); they are NaN for non-numeric values.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    if cand.dtype.kind not in "biufc" or ref.dtype.kind not in "biufc":
        nan = np.full(ref.shape, np.nan)
        return nan, nan, nan
    work = complex if "c" in (cand.dtype.kind, ref.dtype.kind) else float
    with np.errstate(all="ignore"):
        magnitude = np.abs(ref.astype(work))
        abs_err = np.abs(cand.astype(work) - ref.astype(work))
        rel_err = abs_err / magnitude
    return abs_err, rel_err, magnitude


def _applied_rule(
    rule: Mapping[str, Any] | None, output: str, environment: str
) -> dict[str, Any]:
    """Return the rule applied to *output*, in report form.

    Args:
        rule: Declared comparison rule (``{"rule": "exact"}`` or a tolerance
         rule with per-output tolerances), or None for the exact rule.
        output: Output role compared.
        environment: ``same``, ``different`` or ``unknown`` environment.

    Raises:
        ValueError: If the declared rule is malformed.
    """
    if rule is None or dict(rule) == {"rule": "exact"}:
        return {"kind": "exact"}
    if rule.get("rule") != "tolerance" or not isinstance(rule.get("outputs"), Mapping):
        raise ValueError(f"Invalid comparison rule: {rule!r}")
    declared = {
        "declared": dict(rule),
        "output": output,
        "environment": environment,
    }
    tol = rule["outputs"].get(output)
    if tol is None or (
        environment == "same" and rule.get("same_environment") == "exact"
    ):
        return {"kind": "exact", **declared}
    atol, rtol = float(tol["atol"]), float(tol["rtol"])
    if not (atol >= 0 and rtol >= 0):
        raise ValueError(f"Invalid tolerance for output {output!r}")
    return {"kind": "tolerance", "atol": atol, "rtol": rtol, **declared}


def compare_rows(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    rule: Mapping[str, Any] | None = None,
    *,
    output: str = "result",
    environment: str = "unknown",
) -> dict[str, Any]:
    """Compare candidate rows with reference rows under a declared rule.

    The rule comes from the qualification of the operation contract, as plain
    data: ``{"rule": "exact"}`` (the default) or ``{"rule": "tolerance",
    "outputs": {role: {"atol", "rtol"}}, "same_environment": "exact" |
    "tolerance", "justification": ...}``. A tolerance applies only to the
    outputs it names; within the same environment, a rule whose
    ``same_environment`` is ``exact`` is applied exactly. A value matches when it
    equals the reference (NaN at the same positions; for complex values, both
    parts) or, under a tolerance, when ``|c - r| <= atol + rtol * |r|``.

    Args:
        candidate: Row name -> array (e.g. ``{"x": ..., "y": ...}``).
        reference: Row name -> array.
        rule: Declared comparison rule, or None for the exact rule.
        output: Output role compared (selects the declared tolerance).
        environment: ``match`` of :func:`compare_environments`.

    Returns:
        ``{"verdict", "rule", "observed"}``: verdict ``exact``,
        ``within_tolerance`` or ``different``; the applied rule; the mismatch
        count, the maximum absolute and relative errors (None when the error is
        undefined for at least one mismatch), the number of mismatches whose
        error is undefined and, under a tolerance, the number of values out of
        tolerance.

    Raises:
        ValueError: If the declared rule is malformed.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    applied = _applied_rule(rule, output, environment)
    tolerant = applied["kind"] == "tolerance"
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
        return {"verdict": "different", "rule": applied, "observed": observed}
    mismatches = out_of_tolerance = 0
    abs_errors: list[Any] = []
    rel_errors: list[Any] = []
    for key in reference:
        cand = np.asarray(candidate[key])
        ref = np.asarray(reference[key])
        differ = ~_equal(cand, ref)
        count = int(np.count_nonzero(differ))
        mismatches += count
        if count:
            abs_err, rel_err, magnitude = _errors(cand[differ], ref[differ])
            abs_errors.append(abs_err)
            rel_errors.append(rel_err)
            if tolerant:
                with np.errstate(invalid="ignore"):
                    within = np.isfinite(abs_err) & (
                        abs_err <= applied["atol"] + applied["rtol"] * magnitude
                    )
                out_of_tolerance += int(np.count_nonzero(~within))
    # A maximum is given only when the error is defined (finite) for every
    # mismatch (NaN placed differently, infinities or a zero reference leave it
    # undefined); the number of such mismatches is reported.
    max_abs: float | None = 0.0
    max_rel: float | None = 0.0
    abs_undefined = rel_undefined = 0
    if abs_errors:
        all_abs, all_rel = np.concatenate(abs_errors), np.concatenate(rel_errors)
        abs_undefined = int(np.count_nonzero(~np.isfinite(all_abs)))
        rel_undefined = int(np.count_nonzero(~np.isfinite(all_rel)))
        max_abs = None if abs_undefined else float(all_abs.max())
        max_rel = None if rel_undefined else float(all_rel.max())
    observed = {
        "mismatches": mismatches,
        "max_abs_error": max_abs,
        "max_rel_error": max_rel,
        "undefined_abs_errors": abs_undefined,
        "undefined_rel_errors": rel_undefined,
        "dtype_match": dtype_match,
        "shape_match": shape_match,
    }
    if tolerant:
        observed["out_of_tolerance"] = out_of_tolerance
    if mismatches == 0 and dtype_match:
        verdict = "exact"
    elif tolerant and dtype_match and out_of_tolerance == 0:
        verdict = "within_tolerance"
    else:
        verdict = "different"
    return {"verdict": verdict, "rule": applied, "observed": observed}


def artifact_content(
    kind: str, value: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the structure and the numeric rows of an analysis result.

    *value* is the stored form of a ``table`` or ``geometry`` result (as written
    by Sigima's ``to_dict``): a table has ``names``, ``data`` and
    ``roi_indices``; a geometry has a shape ``kind``, ``coords`` and
    ``roi_indices``. Titles, function names and attributes (display preferences,
    parameters) are informative and not compared.

    Returns:
        ``(structure, rows)``: the structure (kinds, column names, ROI indices)
        must be equal; the rows (``data`` or ``coords``, as float64 arrays) are
        compared under the rule. Non-numeric table data belong to the structure.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    roi = value.get("roi_indices")
    structure: dict[str, Any] = {
        "result_kind": kind,
        "kind": value.get("kind"),
        "roi_indices": None
        if roi is None
        else [int(i) for i in np.asarray(roi).ravel()],
    }
    name = "coords" if kind == "geometry" else "data"
    if kind != "geometry":
        structure["names"] = [str(n) for n in value.get("names") or []]
    raw = value.get(name)
    try:
        rows = {name: np.asarray([] if raw is None else raw, dtype=np.float64)}
    except (TypeError, ValueError):
        structure[name] = np.asarray(raw, dtype=object).tolist()
        rows = {}
    return structure, rows


def compare_artifacts(
    kind: str,
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    rule: Mapping[str, Any] | None = None,
    *,
    output: str = "result",
    environment: str = "unknown",
) -> dict[str, Any]:
    """Compare two analysis results (stored forms) under a declared rule.

    The numeric values are compared as by :func:`compare_rows`; a different
    structure (see :func:`artifact_content`) gives the verdict ``different``.

    Args:
        kind: ``table`` or ``geometry``.
        candidate: Stored form of the candidate result.
        reference: Stored form of the recorded result.
        rule: Declared comparison rule, or None for the exact rule.
        output: Output role compared (selects the declared tolerance).
        environment: ``match`` of :func:`compare_environments`.

    Returns:
        ``{"verdict", "rule", "observed"}``; ``observed`` also tells whether the
        structures match (``structure_match``).
    """
    cand_structure, cand_rows = artifact_content(kind, candidate)
    ref_structure, ref_rows = artifact_content(kind, reference)
    comparison = compare_rows(
        cand_rows, ref_rows, rule, output=output, environment=environment
    )
    structure_match = cand_structure == ref_structure
    comparison["observed"]["structure_match"] = structure_match
    if not structure_match:
        comparison["verdict"] = "different"
    return comparison


def compare_exact(
    candidate: Mapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare candidate rows with reference rows under the ``exact`` rule.

    Same as :func:`compare_rows` without a declared rule.

    Args:
        candidate: Row name -> array (e.g. ``{"x": ..., "y": ...}``).
        reference: Row name -> array.

    Returns:
        ``{"verdict", "rule", "observed"}``.
    """
    return compare_rows(candidate, reference)


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
    outputs: list[dict[str, Any]] | None = None,
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
        outputs: Comparison of each output of the activity, by role (``role``,
         ``recorded``, ``status``, ``verdict``, ``rule``, ``observed`` and
         ``state_id``, ``artifact`` or ``detail`` when relevant), when a
         verification run took place. The verdict of the report is then the
         most severe of their verdicts, and its rule and observations those of
         the first compared output; *comparison* is not used.

    Returns:
        The report.

    Raises:
        ReportError: If the report is invalid.
    """
    if outputs is not None:
        compared = next((o for o in outputs if o["rule"] is not None), {})
        verdict = max(
            (o["verdict"] for o in outputs), key=_SEVERITY.index, default="not_verified"
        )
        rule, observed = compared.get("rule"), compared.get("observed")
    elif (
        comparison is not None
        and reference is not None
        and reference["status"] == "available"
    ):
        verdict = comparison["verdict"]
        rule, observed = comparison["rule"], comparison["observed"]
    else:
        verdict, rule, observed = "not_verified", None, None
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
        "verdict": verdict,
        "rule": rule,
        "observed": observed,
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
    if outputs is not None:
        report["outputs"] = outputs
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
