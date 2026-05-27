"""Parse the `from` / `to` query params into UTC nanoseconds.

Accepts ISO-8601 (with optional `Z` suffix) or epoch-nanoseconds (as a string
or int). Used by the data, exports, and (eventually) search endpoints.
"""

import time
from datetime import UTC, datetime

from canopy.core.errors import BadRequest


def parse_time(value: str | int | None) -> int | None:
    """Parse an input value into UTC nanoseconds.

    `None` → `None` (caller decides default). Empty string → `None` likewise.
    Raises `BadRequest` on malformed input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise BadRequest(f"Invalid time value: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = int(dt.timestamp())
    return secs * 1_000_000_000 + dt.microsecond * 1000


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
