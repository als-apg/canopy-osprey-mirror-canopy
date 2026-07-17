"""parse_time: bound-checking so extreme timestamps 400 instead of 500."""

import pytest

from canopy.core.errors import BadRequest
from canopy.core.time_parse import parse_time


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
