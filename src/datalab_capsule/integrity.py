# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Integrity primitives: RFC 8785 canonical JSON, digests and state fingerprints.

The ``datalab-signal-v1`` fingerprint covers the scientific content of a signal
(dtype, length, rows, units), never its title, colours or display identifiers.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any

__all__ = [
    "SIGNAL_FINGERPRINT_SCHEME",
    "CanonicalJSONError",
    "ProvenanceError",
    "canonical_json",
    "canonical_json_bytes",
    "json_digest",
    "sha256_digest",
    "signal_fingerprint",
    "signal_state_facts",
]

SIGNAL_FINGERPRINT_SCHEME = "datalab-signal-v1"

# Largest integer exactly representable as an IEEE-754 double.
_MAX_SAFE_INTEGER = 2**53


class ProvenanceError(Exception):
    """Base class of every DataLab-Capsule error."""


class CanonicalJSONError(ProvenanceError, ValueError):
    """Raised when a value cannot be serialised as RFC 8785 canonical JSON."""


def _format_number(value: float) -> str:
    """Return the ECMAScript ``Number.prototype.toString`` form of *value*."""
    if math.isnan(value) or math.isinf(value):
        raise CanonicalJSONError("Non-finite numbers are not allowed in JSON")
    if value == 0.0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() yields the shortest round-trip digits, as ECMAScript requires.
    dec = Decimal(repr(abs(value))).normalize()
    _sign, digits_tuple, exponent = dec.as_tuple()
    digits = "".join(str(d) for d in digits_tuple)
    k = len(digits)
    n = k + exponent
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        exp = n - 1
        exp_text = ("+" if exp >= 0 else "-") + str(abs(exp))
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        text = mantissa + "e" + exp_text
    return sign + text


def _format_string(value: str) -> str:
    """Return the JSON string literal of *value* (ECMAScript escaping rules)."""
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\f":
            out.append("\\f")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise CanonicalJSONError("Lone surrogates are not allowed in JSON")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise CanonicalJSONError(f"Integer {value} exceeds the IEEE-754 range")
        out.append(_format_number(float(value)))
    elif isinstance(value, float):
        out.append(_format_number(value))
    elif isinstance(value, str):
        out.append(_format_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalJSONError("Object keys must be strings")
        # RFC 8785 sorts keys by their UTF-16 code units.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        out.append("{")
        for index, key in enumerate(keys):
            if index:
                out.append(",")
            out.append(_format_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise CanonicalJSONError(f"Unsupported JSON type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialise *value* as RFC 8785 (JCS) canonical JSON text.

    Args:
        value: JSON-compatible value (dict, list, str, int, float, bool, None).

    Returns:
        Canonical JSON text.

    Raises:
        CanonicalJSONError: If *value* holds a non-finite float, a lone
         surrogate, a non-string key, an unsafe integer or an unsupported type.
    """
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the UTF-8 bytes of :func:`canonical_json`."""
    return canonical_json(value).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    """Return ``"sha256:<lowercase hex>"`` for *data*."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def json_digest(value: Any) -> str:
    """Return the SHA-256 digest of the canonical JSON form of *value*."""
    return sha256_digest(canonical_json_bytes(value))


def _row_bytes(row: Any, dtype: Any) -> bytes:
    import numpy as np  # pylint: disable=import-outside-toplevel

    little = np.dtype(dtype).newbyteorder("<")
    return np.ascontiguousarray(row, dtype=little).tobytes()


def _present_uncertainty(row: Any) -> bool:
    import numpy as np  # pylint: disable=import-outside-toplevel

    if row is None:
        return False
    arr = np.asarray(row)
    if arr.dtype.kind in "fc":
        return not bool(np.all(np.isnan(arr)))
    return True


def signal_fingerprint(
    x: Any,
    y: Any,
    dx: Any = None,
    dy: Any = None,
    xunit: str | None = "",
    yunit: str | None = "",
) -> str:
    """Return the ``datalab-signal-v1`` fingerprint of a signal.

    Args:
        x: X row (1-D array).
        y: Y row (1-D array).
        dx: Optional X uncertainty row; ignored when absent or all NaN.
        dy: Optional Y uncertainty row; ignored when absent or all NaN.
        xunit: X unit (``None`` is treated as ``""``).
        yunit: Y unit (``None`` is treated as ``""``).

    Returns:
        ``"sha256:<hex>"`` digest.

    Raises:
        ValueError: If rows are not 1-D, differ in length or in dtype.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    names = ["x", "y"]
    rows = [np.asarray(x), np.asarray(y)]
    for name, row in (("dx", dx), ("dy", dy)):
        if _present_uncertainty(row):
            names.append(name)
            rows.append(np.asarray(row))
    dtype = rows[0].dtype.newbyteorder("<")
    length = rows[0].shape[0] if rows[0].ndim == 1 else -1
    for row in rows:
        if row.ndim != 1 or row.shape[0] != length:
            raise ValueError("Signal rows must be 1-D arrays of equal length")
        if row.dtype.newbyteorder("<") != dtype:
            raise ValueError("Signal rows must share one dtype")
    descriptor = {
        "scheme": SIGNAL_FINGERPRINT_SCHEME,
        "dtype": dtype.str,
        "length": length,
        "rows": names,
        "units": {"x": xunit or "", "y": yunit or ""},
    }
    payload = [canonical_json_bytes(descriptor), b"\x00"]
    payload.extend(_row_bytes(row, dtype) for row in rows)
    return sha256_digest(b"".join(payload))


def signal_state_facts(obj: Any) -> dict[str, Any]:
    """Return the state facts of a signal-like object (duck-typed).

    The object must expose ``x``, ``y``, ``dx``, ``dy``, ``xunit``, ``yunit``
    and ``roi``, as Sigima's ``SignalObj`` does. An object with an ROI,
    uncertainty rows or a complex dtype gets a ``None`` fingerprint and the
    reasons are listed in ``limits``.

    Args:
        obj: Signal-like object.

    Returns:
        Dictionary with ``kind``, ``fingerprint``, ``dtype``, ``length``,
        ``rows``, ``units`` and ``limits`` keys.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    x, y = np.asarray(obj.x), np.asarray(obj.y)
    rows = ["x", "y"]
    limits = []
    dx, dy = getattr(obj, "dx", None), getattr(obj, "dy", None)
    if _present_uncertainty(dx):
        rows.append("dx")
    if _present_uncertainty(dy):
        rows.append("dy")
    if len(rows) > 2:
        limits.append("uncertainty")
    if getattr(obj, "roi", None) is not None:
        limits.append("roi")
    if y.dtype.kind == "c" or x.dtype.kind == "c":
        limits.append("complex_dtype")
    units = {"x": obj.xunit or "", "y": obj.yunit or ""}
    fingerprint = None
    if not limits:
        fingerprint = {
            "scheme": SIGNAL_FINGERPRINT_SCHEME,
            "value": signal_fingerprint(x, y, None, None, units["x"], units["y"]),
        }
    return {
        "kind": "signal",
        "fingerprint": fingerprint,
        "dtype": y.dtype.name,
        "length": int(y.shape[0]),
        "rows": rows,
        "units": units,
        "limits": limits,
    }
