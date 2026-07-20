"""Parse the `from` / `to` query params into UTC nanoseconds.

Accepts ISO-8601 (with optional `Z` suffix) or epoch-nanoseconds (as a string
or int). Used by the data, exports, and (eventually) search endpoints.
"""

import time
from datetime import UTC, datetime

from canopy.core.errors import BadRequest

# Epoch-nanoseconds must fit a signed 64-bit range and be non-negative. Beyond
# this, downstream datetime.fromtimestamp / int64 conversions raise OverflowError
# / OSError and surface as 500s; reject at the boundary as a clean 400 instead.
_MIN_NS = 0
_MAX_NS = 9_223_372_036_854_775_807  # int64 max (~year 2262)


def _bounded(ns: int, original: object) -> int:
    if ns < _MIN_NS or ns > _MAX_NS:
        raise BadRequest(f"Time out of range: {original!r}")
    return ns


def parse_time(value: str | int | None) -> int | None:
    """Parse an input value into UTC nanoseconds.

    `None` → `None` (caller decides default). Empty string → `None` likewise.
    Raises `BadRequest` on malformed or out-of-range input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return _bounded(value, value)
    try:
        return _bounded(int(value), value)
    except ValueError:
        pass
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise BadRequest(f"Invalid time value: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        secs = int(dt.timestamp())
    except (OverflowError, OSError) as exc:
        raise BadRequest(f"Time out of range: {value!r}") from exc
    return _bounded(secs * 1_000_000_000 + dt.microsecond * 1000, value)


def now_ns() -> int:
    return time.time_ns()


def parse_bin_size_to_ns(value: str) -> int:
    """Parse a `binSize` query value into nanoseconds.

    v1 accepts integer nanoseconds (as a string). ISO-8601 durations are not
    supported yet — the frontend can compute ns. Raises `BadRequest` on malformed input.
    """
    try:
        ns = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"Invalid binSize: {value!r}; expected nanoseconds as integer") from exc
    if ns < 1:
        raise BadRequest("binSize must be >= 1 ns")
    return ns
