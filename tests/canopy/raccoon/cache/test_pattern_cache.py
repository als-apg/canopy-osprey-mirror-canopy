"""PatternCache: keying on every CacheKey field, TTL, eviction."""

from canopy.raccoon.cache.keys import CacheKey, NameKind
from canopy.raccoon.cache.pattern_cache import PatternCache


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _cache(*, max_entries: int = 100, ttl: float = 300.0) -> tuple[PatternCache, FakeClock]:
    clk = FakeClock()
    return PatternCache(max_entries=max_entries, ttl_s=ttl, clock=clk), clk


def _key(pattern: str, **kw) -> CacheKey:
    return CacheKey(kind=NameKind.PV, pattern=pattern, **kw)


def test_miss_then_hit_returns_a_copy() -> None:
    cache, _ = _cache()
    key = _key("BPMS:%")
    assert cache.get(key) is None

    cache.set(key, ["BPMS:1", "BPMS:2"])
    out = cache.get(key)
    assert out == ["BPMS:1", "BPMS:2"]
    out.append("BPMS:3")
    # Mutation of returned list does not poison the cache.
    assert cache.get(key) == ["BPMS:1", "BPMS:2"]


def test_distinct_keys_are_distinct_entries() -> None:
    cache, _ = _cache()
    a = _key("X", tag="L1")
    b = _key("X", tag="L2")
    c = _key("X", sort_by="z")
    cache.set(a, ["a1"])
    cache.set(b, ["b1"])
    cache.set(c, ["c1"])
    assert cache.get(a) == ["a1"]
    assert cache.get(b) == ["b1"]
    assert cache.get(c) == ["c1"]


def test_entry_expires_after_ttl() -> None:
    cache, clk = _cache(ttl=5.0)
    cache.set(_key("X"), ["x1"])
    clk.t = 4.99
    assert cache.get(_key("X")) == ["x1"]
    clk.t = 5.01
    assert cache.get(_key("X")) is None


def test_lru_eviction_at_capacity() -> None:
    cache, _ = _cache(max_entries=2)
    cache.set(_key("A"), ["a"])
    cache.set(_key("B"), ["b"])
    cache.set(_key("C"), ["c"])
    assert cache.get(_key("A")) is None
    assert cache.evictions == 1
