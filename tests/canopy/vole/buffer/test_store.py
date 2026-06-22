"""Unit tests for PVStore."""

import pytest

from canopy.vole.config import VoleConfig
from canopy.vole.buffer.store import PVStore


@pytest.fixture
def config():
    return VoleConfig(max_points=100, global_bytes_max=100_000)


@pytest.fixture
def store(config):
    return PVStore(config)


class TestGetOrCreate:
    def test_creates_new_ring(self, store):
        ring = store.get_or_create("PV:A")
        assert ring is not None
        assert store.contains("PV:A")
        assert store.ring_count == 1

    def test_returns_existing_ring(self, store):
        ring1 = store.get_or_create("PV:A")
        ring2 = store.get_or_create("PV:A")
        assert ring1 is ring2

    def test_custom_ceilings(self, store):
        ring = store.get_or_create("PV:A", max_points=50)
        assert ring.max_points == 50


class TestDrop:
    def test_drop_existing(self, store):
        store.get_or_create("PV:A")
        assert store.drop("PV:A") is True
        assert not store.contains("PV:A")
        assert store.ring_count == 0

    def test_drop_nonexistent(self, store):
        assert store.drop("PV:X") is False


class TestAccounting:
    def test_total_bytes_and_points(self, store):
        from canopy.contracts.sample import AlarmSeverity
        from canopy.vole.buffer.ring import StoredSample

        ring = store.get_or_create("PV:A")
        ring.append(
            StoredSample(ts_ns=100, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live"),
            now_ns=100,
        )
        assert store.total_points == 1
        assert store.total_bytes == 80


class TestLRU:
    def test_lru_candidates_order(self, store):
        ring_a = store.get_or_create("PV:A")
        ring_b = store.get_or_create("PV:B")
        ring_c = store.get_or_create("PV:C")

        ring_a.touch(100)
        ring_b.touch(300)
        ring_c.touch(200)

        candidates = store.lru_candidates()
        names = [name for name, _ in candidates]
        assert names == ["PV:A", "PV:C", "PV:B"]
