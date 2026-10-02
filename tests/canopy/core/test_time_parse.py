"""parse_time: bound-checking so extreme timestamps 400 instead of 500.

Also ``to_utc`` (naive means UTC) and ``DateTimeBound`` (no bare dates as range bounds).
"""

from typing import Annotated
from datetime import UTC, datetime, timezone, timedelta

import pytest
from fastapi import Query, FastAPI
from fastapi.testclient import TestClient

from canopy.core.errors import BadRequest
from canopy.core.time_parse import DateTimeBound, to_utc, parse_time, reject_date_only


class TestParseTimeBounds:
    def test_epoch_ns_passthrough(self):
        assert parse_time("1700000000000000000") == 1_700_000_000_000_000_000
        assert parse_time(1_700_000_000_000_000_000) == 1_700_000_000_000_000_000

    def test_none_and_empty(self):
        assert parse_time(None) is None
        assert parse_time("") is None

    @pytest.mark.parametrize("value", ["99999999999999999999", "-99999999999999999999", -1])
    def test_out_of_range_rejected(self, value):
        # These used to reach datetime.fromtimestamp and raise OverflowError/OSError
        # (a 500). Now a clean 400.
        with pytest.raises(BadRequest, match="out of range"):
            parse_time(value)

    def test_iso_still_works(self):
        assert parse_time("2024-01-01T00:00:00Z") == 1_704_067_200_000_000_000

    def test_far_future_iso_rejected(self):
        with pytest.raises(BadRequest):
            parse_time("9999-01-01T00:00:00Z")

    def test_garbage_rejected(self):
        with pytest.raises(BadRequest, match="Invalid time"):
            parse_time("not-a-time")

    def test_offset_iso_matches_its_utc_instant(self):
        assert parse_time("2023-12-31T16:00:00-08:00") == parse_time("2024-01-01T00:00:00Z")

    def test_offset_that_overflows_when_converted_is_rejected(self):
        with pytest.raises(BadRequest, match="out of range"):
            parse_time("9999-12-31T23:00:00-05:00")


class TestToUtc:
    def test_naive_value_is_read_as_utc(self):
        assert to_utc(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def test_utc_value_is_unchanged(self):
        value = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert to_utc(value) == value

    def test_offset_value_is_converted_to_utc(self):
        converted = to_utc(datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7))))
        assert converted == datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
        assert converted.utcoffset() == timedelta(0)


class TestRejectDateOnly:
    @pytest.mark.parametrize("value", ["2026-01-31", " 2026-01-31 "])
    def test_bare_date_is_refused(self, value):
        with pytest.raises(ValueError, match="not a bare date"):
            reject_date_only(value)

    @pytest.mark.parametrize("value", ["2026-01-31T23:59:59Z", "2026-01-31T00:00:00", None, 1_700_000_000])
    def test_anything_else_passes_through(self, value):
        assert reject_date_only(value) == value


@pytest.fixture
def bound_client() -> TestClient:
    """A throwaway app with one ``DateTimeBound`` query parameter that echoes what it parsed."""
    app = FastAPI()

    @app.get("/echo")
    async def echo(at: Annotated[DateTimeBound, Query()] = None) -> dict[str, str | None]:
        return {"at": at.isoformat() if at else None}

    return TestClient(app)


class TestDateTimeBound:
    def test_omitted_bound_is_none(self, bound_client):
        response = bound_client.get("/echo")
        assert response.status_code == 200
        assert response.json() == {"at": None}

    def test_datetime_bound_is_parsed(self, bound_client):
        response = bound_client.get("/echo", params={"at": "2026-01-31T23:59:59Z"})
        assert response.status_code == 200
        assert response.json() == {"at": "2026-01-31T23:59:59+00:00"}

    def test_bare_date_bound_is_a_422(self, bound_client):
        response = bound_client.get("/echo", params={"at": "2026-01-31"})
        assert response.status_code == 422
        assert "not a bare date" in response.text

    def test_malformed_bound_is_a_422(self, bound_client):
        assert bound_client.get("/echo", params={"at": "not-a-date"}).status_code == 422
