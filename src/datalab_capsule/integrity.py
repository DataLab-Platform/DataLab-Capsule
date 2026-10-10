# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Integrity primitives: RFC 8785 canonical JSON, digests and state fingerprints.

The ``datalab-signal-v1`` and ``datalab-image-v1`` fingerprints cover the
scientific content of a signal or an image (dtype, shape, rows or coordinates,
units), never its title, colours or display identifiers.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any

__all__ = [
    "IMAGE_FINGERPRINT_SCHEME",
    "SIGNAL_FINGERPRINT_SCHEME",
    "CanonicalJSONError",
    "ProvenanceError",
    "canonical_json",
    "canonical_json_bytes",
    "image_fingerprint",
    "image_state_facts",
    "json_digest",
    "roi_facts",
    "sha256_digest",
    "signal_fingerprint",
    "signal_state_facts",
    "state_facts",
]

SIGNAL_FINGERPRINT_SCHEME = "datalab-signal-v1"
IMAGE_FINGERPRINT_SCHEME = "datalab-image-v1"

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
    and ``roi``, as Sigima's ``SignalObj`` does. Uncertainty rows are part of the
    fingerprint; an ROI is recorded under ``roi`` (see :func:`roi_facts`). A
    complex signal gets a ``None`` fingerprint and the reason is listed in
    ``limits``.

    Args:
        obj: Signal-like object.

    Returns:
        Dictionary with ``kind``, ``fingerprint``, ``dtype``, ``length``,
        ``rows``, ``units`` and ``limits`` keys, and ``roi`` when the object
        has one.
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
    if y.dtype.kind == "c" or x.dtype.kind == "c":
        limits.append("complex_dtype")
    units = {"x": obj.xunit or "", "y": obj.yunit or ""}
    fingerprint = None
    if not limits:
        fingerprint = {
            "scheme": SIGNAL_FINGERPRINT_SCHEME,
            "value": signal_fingerprint(
                x,
                y,
                dx if "dx" in rows else None,
                dy if "dy" in rows else None,
                units["x"],
                units["y"],
            ),
        }
    facts = {
        "kind": "signal",
        "fingerprint": fingerprint,
        "dtype": y.dtype.name,
        "length": int(y.shape[0]),
        "rows": rows,
        "units": units,
        "limits": limits,
    }
    roi = roi_facts(getattr(obj, "roi", None))
    if roi is not None:
        facts["roi"] = roi
    return facts


def _json_ready(value: Any) -> Any:
    """Return *value* with NumPy scalars and arrays turned into JSON values."""
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if hasattr(value, "tolist"):
        return _json_ready(value.tolist())
    return value


def roi_facts(roi: Any) -> dict[str, Any] | None:
    """Return the recorded facts of a region of interest (duck-typed), or None.

    The ROI must expose ``to_dict()``, as Sigima's ROI classes do. Masks are
    derived from the ROI, so they are covered by its definition.

    Returns:
        ``{"definition", "digest"}``; the digest identifies the definition.
    """
    if roi is None:
        return None
    definition = _json_ready(roi.to_dict())
    return {"definition": definition, "digest": json_digest(definition)}


def image_fingerprint(
    data: Any,
    *,
    x0: float = 0.0,
    y0: float = 0.0,
    dx: float = 1.0,
    dy: float = 1.0,
    xcoords: Any = None,
    ycoords: Any = None,
    xunit: str | None = "",
    yunit: str | None = "",
    zunit: str | None = "",
) -> str:
    """Return the ``datalab-image-v1`` fingerprint of an image.

    The payload is the canonical JSON descriptor (scheme, dtype, shape,
    coordinates, units), a NUL byte, the data in C order and little-endian
    byte order, then, for non-uniform coordinates, the X and Y coordinates as
    little-endian float64.

    Args:
        data: 2-D array.
        x0: X origin (uniform coordinates).
        y0: Y origin (uniform coordinates).
        dx: X pixel size (uniform coordinates).
        dy: Y pixel size (uniform coordinates).
        xcoords: X coordinates; non-uniform coordinates when both are given.
        ycoords: Y coordinates.
        xunit: X unit (``None`` is treated as ``""``).
        yunit: Y unit (``None`` is treated as ``""``).
        zunit: Z unit (``None`` is treated as ``""``).

    Returns:
        ``"sha256:<hex>"`` digest.

    Raises:
        ValueError: If *data* is not 2-D or coordinates do not match its shape.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    array = np.asarray(data)
    if array.ndim != 2:
        raise ValueError("Image data must be a 2-D array")
    dtype = array.dtype.newbyteorder("<")
    uniform = xcoords is None or ycoords is None
    if uniform:
        coords: dict[str, Any] = {
            "x0": float(x0),
            "y0": float(y0),
            "dx": float(dx),
            "dy": float(dy),
        }
        extra: list[bytes] = []
    else:
        xc, yc = np.asarray(xcoords), np.asarray(ycoords)
        if xc.shape != (array.shape[1],) or yc.shape != (array.shape[0],):
            raise ValueError("Image coordinates must match the data shape")
        coords = {"x": int(xc.size), "y": int(yc.size)}
        extra = [_row_bytes(xc, np.float64), _row_bytes(yc, np.float64)]
    descriptor = {
        "scheme": IMAGE_FINGERPRINT_SCHEME,
        "dtype": dtype.str,
        "shape": [int(n) for n in array.shape],
        "coords": {"uniform" if uniform else "nonuniform": coords},
        "units": {"x": xunit or "", "y": yunit or "", "z": zunit or ""},
    }
    payload = [canonical_json_bytes(descriptor), b"\x00", _row_bytes(array, dtype)]
    payload.extend(extra)
    return sha256_digest(b"".join(payload))


def image_state_facts(obj: Any) -> dict[str, Any]:
    """Return the state facts of an image-like object (duck-typed).

    The object must expose ``data``, ``is_uniform_coords``, ``x0``, ``y0``,
    ``dx``, ``dy``, ``xcoords``, ``ycoords``, ``xunit``, ``yunit``, ``zunit``
    and ``roi``, as Sigima's ``ImageObj`` does. A complex image gets a ``None``
    fingerprint and the reason is listed in ``limits``.
    """
    import numpy as np  # pylint: disable=import-outside-toplevel

    data = np.asarray(obj.data)
    units = {"x": obj.xunit or "", "y": obj.yunit or "", "z": obj.zunit or ""}
    limits = ["complex_dtype"] if data.dtype.kind == "c" else []
    fingerprint = None
    if not limits:
        if obj.is_uniform_coords:
            coords = {"x0": obj.x0, "y0": obj.y0, "dx": obj.dx, "dy": obj.dy}
        else:
            coords = {"xcoords": obj.xcoords, "ycoords": obj.ycoords}
        fingerprint = {
            "scheme": IMAGE_FINGERPRINT_SCHEME,
            "value": image_fingerprint(
                data, xunit=units["x"], yunit=units["y"], zunit=units["z"], **coords
            ),
        }
    facts = {
        "kind": "image",
        "fingerprint": fingerprint,
        "dtype": data.dtype.name,
        "shape": [int(n) for n in data.shape],
        "rows": ["data"],
        "units": units,
        "limits": limits,
    }
    roi = roi_facts(getattr(obj, "roi", None))
    if roi is not None:
        facts["roi"] = roi
    return facts


def state_facts(obj: Any) -> dict[str, Any]:
    """Return the state facts of a signal-like or image-like object."""
    if hasattr(obj, "data") and not hasattr(obj, "xydata"):
        return image_state_facts(obj)
    return signal_state_facts(obj)
