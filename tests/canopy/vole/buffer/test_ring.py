"""Unit tests for PerPVRing."""

from canopy.contracts.sample import AlarmSeverity
from canopy.vole.buffer.ring import PerPVRing, StoredSample


def _sample(ts_ns: int, value: float = 1.0, source: str = "live") -> StoredSample:
    return StoredSample(ts_ns=ts_ns, value=value, severity=AlarmSeverity.NO_ALARM, source=source)


class TestAppend:
    def test_append_in_order(self):
        ring = PerPVRing(max_points=100)
        assert ring.append(_sample(100), now_ns=100) is True
        assert ring.append(_sample(200), now_ns=200) is True
        assert ring.count == 2

    def test_append_out_of_order_rejected(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(200), now_ns=200)
        assert ring.append(_sample(100), now_ns=200) is False
        assert ring.count == 1
        assert ring.dropped_out_of_order == 1

    def test_append_duplicate_timestamp_rejected(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100), now_ns=100)
        assert ring.append(_sample(100, value=2.0), now_ns=100) is False
        assert ring.count == 1

    def test_append_enforces_max_points(self):
        ring = PerPVRing(max_points=3)
        for i in range(5):
            ring.append(_sample(i * 10), now_ns=i * 10)
        assert ring.count == 3
        assert ring.earliest.ts_ns == 20
        assert ring.latest.ts_ns == 40


class TestSeed:
    def test_seed_inserts_samples(self):
        ring = PerPVRing(max_points=100)
        samples = [_sample(i * 10, source="archive") for i in range(5)]
        count = ring.seed(samples, now_ns=100)
        assert count == 5
        assert ring.count == 5

    def test_seed_rejects_overlap_with_live(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(50, source="live"), now_ns=50)
        archive = [_sample(i * 10, source="archive") for i in range(10)]
        count = ring.seed(archive, now_ns=100)
        # ts=50..90 should be rejected (overlap with live at ts=50)
        assert count == 5  # ts=0,10,20,30,40
        assert ring.latest.ts_ns == 50
        assert ring.latest.source == "live"

    def test_seed_empty_list(self):
        ring = PerPVRing(max_points=100)
        assert ring.seed([], now_ns=100) == 0

    def test_seed_enforces_max_points(self):
        ring = PerPVRing(max_points=5)
        samples = [_sample(i * 10, source="archive") for i in range(20)]
        ring.seed(samples, now_ns=200)
        assert ring.count == 5
        assert ring.earliest.ts_ns == 150
        assert ring.latest.ts_ns == 190


class TestSlice:
    def test_slice_returns_range(self):
        ring = PerPVRing(max_points=100)
        for i in range(10):
            ring.append(_sample(i * 100), now_ns=i * 100)
        result = ring.slice(200, 700)
        assert len(result) == 6
        assert result[0].ts_ns == 200
        assert result[-1].ts_ns == 700

    def test_slice_empty_ring(self):
        ring = PerPVRing(max_points=100)
        assert ring.slice(0, 1000) == []

    def test_slice_no_match(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100), now_ns=100)
        assert ring.slice(200, 300) == []


class TestSampleBefore:
    def test_sample_before_found(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100, value=1.0), now_ns=100)
        ring.append(_sample(200, value=2.0), now_ns=200)
        ring.append(_sample(300, value=3.0), now_ns=300)
        result = ring.sample_before(250)
        assert result is not None
        assert result.ts_ns == 200

    def test_sample_before_none(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100), now_ns=100)
        assert ring.sample_before(50) is None

    def test_sample_before_empty(self):
        ring = PerPVRing(max_points=100)
        assert ring.sample_before(100) is None


class TestProperties:
    def test_latest_and_earliest(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100), now_ns=100)
        ring.append(_sample(200), now_ns=200)
        assert ring.earliest.ts_ns == 100
        assert ring.latest.ts_ns == 200

    def test_empty_ring_properties(self):
        ring = PerPVRing(max_points=100)
        assert ring.latest is None
        assert ring.earliest is None
        assert ring.count == 0
        assert ring.byte_estimate == 0

    def test_touch_updates_last_access(self):
        ring = PerPVRing(max_points=100)
        assert ring.last_access_ns == 0
        ring.touch(500)
        assert ring.last_access_ns == 500

    def test_clear(self):
        ring = PerPVRing(max_points=100)
        ring.append(_sample(100), now_ns=100)
        ring.clear()
        assert ring.count == 0
