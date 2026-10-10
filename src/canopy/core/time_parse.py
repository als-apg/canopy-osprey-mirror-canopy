"""Parse the `from` / `to` query params into UTC nanoseconds.

Accepts ISO-8601 (with optional `Z` suffix) or epoch-nanoseconds (as a string
or int). Used by the data, exports, and (eventually) search endpoints.

Also holds Canopy's one rule for timezone-less datetimes (``to_utc``: a naive
value is read as UTC) and ``DateTimeBound``, the query-parameter type for
endpoints that take a datetime range bound. Declare it as
``Annotated[DateTimeBound, Query(...)] = None``: FastAPI drops the validator
when the ``Query`` is the parameter default instead.
"""

import re
import time
from typing import Annotated
from datetime import UTC, datetime

from pydantic import BeforeValidator

from canopy.core.errors import BadRequest

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")

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
    try:
        dt = to_utc(dt)
        secs = int(dt.timestamp())
    except (OverflowError, OSError) as exc:
        raise BadRequest(f"Time out of range: {value!r}") from exc
    return _bounded(secs * 1_000_000_000 + dt.microsecond * 1000, value)


def to_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Canopy reads a datetime that arrives without an offset as UTC, and converts
    one that carries an offset to UTC, so it can be compared against
    ``timestamptz`` columns and nanosecond timestamps alike.

    Parameters
    ----------
    value : datetime
        A naive or timezone-aware datetime.

    Returns
    -------
    datetime
        The same instant with ``tzinfo`` set to UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def reject_date_only(value: object) -> object:
    """Refuse a bare ``YYYY-MM-DD`` where a date and time are expected.

    Pydantic reads a date-only string as midnight, which silently turns an
    inclusive upper bound such as ``end=2026-01-31`` into one that excludes that
    whole day. Requiring the time makes the caller state which instant it means.

    Parameters
    ----------
    value : object
        The raw query value, before datetime parsing.

    Returns
    -------
    object
        ``value`` unchanged when it is not a bare date.

    Raises
    ------
    ValueError
        When ``value`` is a string holding only a date.
    """
    if isinstance(value, str) and _DATE_ONLY.fullmatch(value.strip()):
        raise ValueError("expected a date and time such as 2026-01-31T23:59:59Z, not a bare date")
    return value


DateTimeBound = Annotated[datetime | None, BeforeValidator(reject_date_only)]


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
