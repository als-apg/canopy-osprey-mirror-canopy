"""The sample-record port the Formula Engine's table evaluator consumes.

One case per field of the record ``last_at_or_before`` returns, the receive-time
enumeration ``samples_since``, the one-step ``ring_slice`` copy, and the measured
``BYTES_PER_SAMPLE`` footprint. The port is exercised through ``VoleDataSource``,
the object ``formula_engine_lifespan`` injects into the Formula Engine, with Vole's real ``BufferService``.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from canopy.formula_engine.ports import RingSlice, SampleRecord
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.buffer.ring import BYTES_PER_SAMPLE, StoredSample, measure_sample_bytes

PV = "SR:BPM:01:X"
S = 1_000_000_000


def _sample(ts_ns: int, value=1.0, severity=AlarmSeverity.NO_ALARM) -> Sample:
    return Sample(ts_ns=ts_ns, value=value, severity=severity, source="live")


@pytest.fixture
def source(test_service) -> VoleDataSource:
    return VoleDataSource(test_service)


@pytest.fixture
async def filled(test_service):
    """Three live samples at 10/20/30 s with distinct receive times, after a seeded one at 5 s."""
    await test_service.seed(PV, [Sample(ts_ns=5 * S, value=0.5, severity=AlarmSeverity.MINOR, source="archive")])
    await test_service.append(PV, _sample(10 * S, 1.0), recv_ns=100, after_connect=True)
    await test_service.append(PV, _sample(20 * S, 2.0, AlarmSeverity.INVALID), recv_ns=200)
    await test_service.append(PV, _sample(30 * S, "3"), recv_ns=300)
    return test_service


class TestLastAtOrBefore:
    async def test_returns_none_before_the_first_sample(self, filled, source):
        assert source.last_at_or_before(PV, 4 * S) is None

    async def test_returns_none_for_an_unknown_pv(self, filled, source):
        assert source.last_at_or_before("NOPE", 4 * S) is None

    async def test_exact_timestamp_hits_that_sample(self, filled, source):
        assert source.last_at_or_before(PV, 20 * S).ts_ns == 20 * S

    async def test_between_samples_picks_the_earlier_one(self, filled, source):
        assert source.last_at_or_before(PV, 25 * S).ts_ns == 20 * S

    async def test_value_is_raw(self, filled, source):
        # Non-numeric values are returned as stored; null-ing them is the table store's cell fill.
        assert source.last_at_or_before(PV, 30 * S).value == "3"
        assert source.last_at_or_before(PV, 10 * S).value == 1.0

    async def test_severity_is_carried(self, filled, source):
        assert source.last_at_or_before(PV, 20 * S).severity is AlarmSeverity.INVALID
        assert source.last_at_or_before(PV, 5 * S).severity is AlarmSeverity.MINOR

    async def test_recv_ns_is_the_receive_time_and_zero_for_archive(self, filled, source):
        assert source.last_at_or_before(PV, 20 * S).recv_ns == 200
        assert source.last_at_or_before(PV, 5 * S).recv_ns == 0

    async def test_after_connect_flag_is_carried(self, filled, source):
        assert source.last_at_or_before(PV, 10 * S).after_connect is True
        assert source.last_at_or_before(PV, 20 * S).after_connect is False

    async def test_in_outage_reflects_a_recorded_interval(self, filled, source):
        rec = source.last_at_or_before(PV, 25 * S)
        assert rec.in_outage is False
        filled.open_outage(PV, 22 * S)
        filled.close_outage(PV, 28 * S)
        assert source.last_at_or_before(PV, 25 * S).in_outage is True
        assert source.last_at_or_before(PV, 29 * S).in_outage is False

    async def test_record_is_the_six_field_named_tuple(self, filled, source):
        rec = source.last_at_or_before(PV, 10 * S)
        assert isinstance(rec, SampleRecord)
        assert rec._fields == ("value", "severity", "in_outage", "ts_ns", "recv_ns", "after_connect")


class TestSamplesSince:
    async def test_returns_only_samples_received_after_the_argument_oldest_first(self, filled, source):
        got = source.samples_since(PV, 100)
        assert [r.ts_ns for r in got] == [20 * S, 30 * S]
        assert [r.recv_ns for r in got] == [200, 300]

    async def test_equal_receive_time_is_excluded(self, filled, source):
        assert [r.ts_ns for r in source.samples_since(PV, 300)] == []

    async def test_stops_at_archive_seeded_samples(self, filled, source):
        # Seeded samples carry recv_ns == 0, so a zero watermark returns every live sample and no seed.
        got = source.samples_since(PV, 0)
        assert [r.ts_ns for r in got] == [10 * S, 20 * S, 30 * S]

    async def test_unknown_pv_is_empty(self, filled, source):
        assert source.samples_since("NOPE", 0) == []


class TestRingSlice:
    async def test_copies_samples_at_or_after_the_argument(self, filled, source):
        rs = source.ring_slice(PV, 20 * S)
        assert isinstance(rs, RingSlice)
        assert rs.ts_ns.dtype == np.int64 and rs.ts_ns.tolist() == [20 * S, 30 * S]
        assert rs.values.dtype == object and rs.values.tolist() == [2.0, "3"]
        assert rs.severities.dtype == np.int8
        assert rs.severities.tolist() == [list(AlarmSeverity).index(AlarmSeverity.INVALID), 0]

    async def test_includes_the_outage_intervals(self, filled, source):
        filled.open_outage(PV, 12 * S)
        filled.close_outage(PV, 15 * S)
        filled.open_outage(PV, 40 * S)
        rs = source.ring_slice(PV, 0)
        assert rs.outages == [(12 * S, 15 * S), (40 * S, None)]

    async def test_is_a_copy(self, filled, source):
        rs = source.ring_slice(PV, 0)
        await filled.append(PV, _sample(40 * S, 4.0), recv_ns=400)
        assert rs.ts_ns.tolist() == [5 * S, 10 * S, 20 * S, 30 * S]

    async def test_unknown_pv_is_empty(self, filled, source):
        rs = source.ring_slice("NOPE", 0)
        assert rs.ts_ns.size == 0 and rs.values.size == 0 and rs.outages == []


class TestBytesPerSample:
    def test_matches_the_measured_six_field_footprint(self):
        sample = StoredSample(
            1_700_000_000_000_000_000, 1.5, AlarmSeverity.NO_ALARM, "live", 1_700_000_000_000_000_000, False
        )
        assert len(sample) == 6
        measured = (
            sys.getsizeof(sample)
            + sys.getsizeof(sample.ts_ns)
            + sys.getsizeof(sample.value)
            + sys.getsizeof(sample.recv_ns)
        )
        assert BYTES_PER_SAMPLE == measure_sample_bytes() == measured
        assert BYTES_PER_SAMPLE > 80

    def test_defaults_keep_four_field_construction_working(self):
        sample = StoredSample(ts_ns=1, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live")
        assert sample.recv_ns == 0 and sample.after_connect is False
