"""PVNameCache: hit/miss accounting, separate TTLs, LRU eviction."""

from canopy.raccoon.cache.name_cache import PVNameCache


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _cache(*, max_entries: int = 100, pos: float = 1800.0, neg: float = 60.0) -> tuple[PVNameCache, FakeClock]:
    clk = FakeClock()
    return PVNameCache(max_entries=max_entries, positive_ttl_s=pos, negative_ttl_s=neg, clock=clk), clk


def test_miss_then_hit() -> None:
    cache, _ = _cache()
    assert cache.get("FOO:1") is None
    assert cache.misses == 1

    cache.set("FOO:1", True)
    assert cache.get("FOO:1") is True
    assert cache.hits == 1


def test_positive_entry_expires_after_positive_ttl() -> None:
    cache, clk = _cache(pos=10.0, neg=2.0)
    cache.set("FOO:1", True)
    clk.t = 9.99
    assert cache.get("FOO:1") is True

    clk.t = 10.01
    assert cache.get("FOO:1") is None


def test_negative_entry_expires_after_negative_ttl() -> None:
    cache, clk = _cache(pos=10.0, neg=2.0)
    cache.set("FOO:1", False)
    clk.t = 1.99
    assert cache.get("FOO:1") is False

    clk.t = 2.01
    assert cache.get("FOO:1") is None


def test_lru_eviction_at_capacity() -> None:
    cache, _ = _cache(max_entries=2)
    cache.set("A", True)
    cache.set("B", True)
    cache.set("C", True)

    assert cache.get("A") is None
    assert cache.evictions == 1
    assert cache.size() == 2


def test_hit_reorders_to_most_recent() -> None:
    cache, _ = _cache(max_entries=2)
    cache.set("A", True)
    cache.set("B", True)
    # Touch A, making B the LRU.
    assert cache.get("A") is True
    cache.set("C", True)

    assert cache.get("B") is None
    assert cache.get("A") is True
    assert cache.get("C") is True


def test_set_replaces_entry() -> None:
    cache, _ = _cache()
    cache.set("FOO:1", False)
    cache.set("FOO:1", True)
    assert cache.get("FOO:1") is True
