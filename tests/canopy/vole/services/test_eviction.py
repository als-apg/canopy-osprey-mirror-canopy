"""Unit tests for EvictionService."""

import pytest

from canopy.vole.config import VoleConfig
from canopy.contracts.sample import AlarmSeverity
from canopy.vole.buffer.ring import StoredSample
from canopy.vole.buffer.store import PVStore
from canopy.vole.services.eviction_service import EvictionService


def _sample(ts_ns: int) -> StoredSample:
    return StoredSample(ts_ns=ts_ns, value=1.0, severity=AlarmSeverity.NO_ALARM, source="live")


@pytest.fixture
def small_config():
    return VoleConfig(
        max_age_ns=1_000_000_000,
        max_points=100,
        global_bytes_max=400,  # 5 samples worth (5 * 80 = 400)
    )


@pytest.fixture
def store_with_data(small_config):
    store = PVStore(small_config)
    ring_a = store.get_or_create("PV:A")
    ring_b = store.get_or_create("PV:B")
    ring_c = store.get_or_create("PV:C")

    for i in range(3):
        ring_a.append(_sample(i * 10), now_ns=i * 10)
    ring_a.touch(100)

    for i in range(3):
        ring_b.append(_sample(i * 10), now_ns=i * 10)
    ring_b.touch(300)

    for i in range(3):
        ring_c.append(_sample(i * 10), now_ns=i * 10)
    ring_c.touch(200)

    return store


class TestPressureDetection:
    def test_under_pressure_when_over_ceiling(self, store_with_data, small_config):
        eviction = EvictionService(store_with_data, small_config)
        # 9 samples * 80 = 720 > 400
        assert eviction.under_pressure() is True

    def test_not_under_pressure_when_below(self, small_config):
        store = PVStore(small_config)
        ring = store.get_or_create("PV:A")
        ring.append(_sample(100), now_ns=100)
        eviction = EvictionService(store, small_config)
        assert eviction.under_pressure() is False


class TestEviction:
    async def test_evict_lru_drops_least_recently_accessed(self, store_with_data, small_config):
        eviction = EvictionService(store_with_data, small_config)

        dropped = await eviction.evict_lru()
        # PV:A has last_access=100 (LRU), PV:C=200, PV:B=300
        # Should drop in LRU order until under 400 bytes
        assert "PV:A" in dropped
        assert store_with_data.total_bytes <= small_config.global_bytes_max

    async def test_evict_lru_returns_empty_when_no_pressure(self, small_config):
        store = PVStore(small_config)
        ring = store.get_or_create("PV:A")
        ring.append(_sample(100), now_ns=100)
        eviction = EvictionService(store, small_config)
        dropped = await eviction.evict_lru()
        assert dropped == []
