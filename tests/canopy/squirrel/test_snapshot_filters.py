"""Unit tests for the snapshot listing datetime bounds — no database involved."""

from datetime import UTC, datetime, timezone, timedelta

from canopy.squirrel.services.snapshot_service import normalize_datetime_bound


def test_none_bound_stays_none():
    assert normalize_datetime_bound(None) is None


def test_naive_bound_is_read_as_utc():
    assert normalize_datetime_bound(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_utc_bound_is_unchanged():
    value = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert normalize_datetime_bound(value) == value


def test_offset_bound_is_converted_to_utc():
    value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7)))
    bound = normalize_datetime_bound(value)
    assert bound == datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    assert bound.utcoffset() == timedelta(0)


def test_every_bound_is_timezone_aware():
    for value in (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))):
        assert normalize_datetime_bound(value).tzinfo is not None
