"""Caching/chaining registry behaviour — the auth contract, without a database.

A fake inner registry stands in for Postgres, so these pin the parts operators
actually depend on: revocation takes effect after the TTL, a store outage does not
black out clients that were already authenticating, and a bounded stale window
means an outage can't keep a revoked key alive forever.
"""

import pytest
from sqlalchemy.exc import OperationalError

from canopy.core.errors import KeyStoreUnavailable
from canopy.core.security import APIKey, EnvKeyRegistry
from canopy.core.keys.tokens import hash_token
from canopy.core.keys.registry import CachingKeyRegistry, ChainedKeyRegistry

TOKEN = "cnpy_token"
OTHER = "cnpy_other"


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


class FakeStore:
    """Stands in for `DbKeyRegistry`: serves a dict, counts lookups, can break."""

    def __init__(self, keys: dict[str, APIKey] | None = None):
        self.keys = keys or {}
        self.calls = 0
        self.broken = False
        self.error_factory = db_error

    async def get(self, token: str) -> APIKey | None:
        self.calls += 1
        if self.broken:
            raise self.error_factory()
        return self.keys.get(token)


def a_key(name: str = "trace", **scopes) -> APIKey:
    return APIKey(token=TOKEN, read=scopes.get("read", True), write=scopes.get("write", False), name=name, id="id-1")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("canopy.core.keys.registry.monotonic", lambda: fake.now)
    return fake


class TestCaching:
    async def test_repeat_lookups_hit_the_cache(self, clock):
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60)

        for _ in range(5):
            assert (await registry.get(TOKEN)).name == "trace"
        assert store.calls == 1

    async def test_revocation_takes_effect_after_the_ttl(self, clock):
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60)
        assert await registry.get(TOKEN) is not None

        store.keys.clear()  # deactivated in the database

        clock.advance(30)
        assert await registry.get(TOKEN) is not None, "still cached inside the TTL"
        clock.advance(31)
        assert await registry.get(TOKEN) is None

    async def test_unknown_token_is_negatively_cached_then_rechecked(self, clock):
        store = FakeStore()
        registry = CachingKeyRegistry(store, negative_ttl_s=5)

        assert await registry.get(TOKEN) is None
        assert await registry.get(TOKEN) is None
        assert store.calls == 1, "a guessing flood must not become a query flood"

        clock.advance(6)
        assert await registry.get(TOKEN) is None
        assert store.calls == 2


class TestStoreOutage:
    async def test_cached_key_survives_an_outage(self, clock):
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60, max_stale_s=3600)
        await registry.get(TOKEN)

        store.broken = True
        clock.advance(120)  # past the TTL, so this is a genuine stale serve

        key = await registry.get(TOKEN)
        assert key is not None and key.name == "trace"

    async def test_stale_key_expires_at_the_stale_horizon(self, clock):
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60, max_stale_s=3600)
        await registry.get(TOKEN)

        store.broken = True
        clock.advance(3601)

        with pytest.raises(KeyStoreUnavailable):
            await registry.get(TOKEN)

    async def test_uncached_token_fails_closed_during_an_outage(self, clock):
        store = FakeStore({TOKEN: a_key()})
        store.broken = True
        registry = CachingKeyRegistry(store)

        # 503, not 401: the token isn't known to be bad, the store is down.
        with pytest.raises(KeyStoreUnavailable):
            await registry.get(TOKEN)

    async def test_refused_connection_takes_the_stale_path(self, clock):
        # asyncpg raises a bare OSError from asyncio when Postgres is not listening;
        # SQLAlchemy never wraps it. Catching only SQLAlchemyError let that escape as
        # a 500 and skipped serve-stale entirely.
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60, max_stale_s=3600)
        await registry.get(TOKEN)

        store.broken = True
        store.error_factory = lambda: OSError("Multiple exceptions: [Errno 61] Connect call failed")
        clock.advance(120)

        key = await registry.get(TOKEN)
        assert key is not None and key.name == "trace"

    async def test_refused_connection_without_cache_raises_key_store_unavailable(self, clock):
        store = FakeStore()
        store.broken = True
        store.error_factory = lambda: OSError("[Errno 61] Connect call failed")

        with pytest.raises(KeyStoreUnavailable):
            await CachingKeyRegistry(store).get(TOKEN)

    async def test_negatives_are_never_served_stale(self, clock):
        store = FakeStore()
        registry = CachingKeyRegistry(store, negative_ttl_s=5)
        assert await registry.get(TOKEN) is None

        store.broken = True
        clock.advance(6)

        with pytest.raises(KeyStoreUnavailable):
            await registry.get(TOKEN)

    async def test_negative_cache_is_bounded(self, clock):
        store = FakeStore()
        registry = CachingKeyRegistry(store, negative_ttl_s=5, max_entries=10)

        for i in range(50):
            await registry.get(f"cnpy_guess{i}")

        assert len(registry._cache) <= 10

    async def test_pruning_drops_entries_past_their_horizon(self, clock):
        store = FakeStore()
        registry = CachingKeyRegistry(store, negative_ttl_s=5, max_entries=1)

        stale_key = hash_token("cnpy_stale")
        await registry.get("cnpy_stale")  # cached negative, about to age out
        assert stale_key in registry._cache

        clock.advance(6)  # past negative_ttl_s: eligible for pruning, but not pruned yet
        assert stale_key in registry._cache

        await registry.get("cnpy_fresh")  # cache is full: forces _prune before storing
        assert stale_key not in registry._cache, "a negative past its horizon must not survive a prune"

    async def test_invalidate_forces_a_fresh_lookup(self, clock):
        store = FakeStore({TOKEN: a_key()})
        registry = CachingKeyRegistry(store, ttl_s=60)
        await registry.get(TOKEN)
        assert store.calls == 1

        store.keys.clear()  # deactivated in the database
        registry.invalidate(TOKEN)

        assert await registry.get(TOKEN) is None, "revocation via invalidate() must not wait out the TTL"
        assert store.calls == 2


class TestChaining:
    async def test_env_keys_work_while_the_store_is_down(self, clock):
        store = FakeStore()
        store.broken = True
        registry = ChainedKeyRegistry(EnvKeyRegistry("envkey:read"), CachingKeyRegistry(store))

        key = await registry.get("envkey")
        assert key is not None and key.read
        assert store.calls == 0, "env is checked first, so a dead store is never consulted"

    async def test_db_keys_resolve_when_env_misses(self, clock):
        store = FakeStore({TOKEN: a_key(name="from-db")})
        registry = ChainedKeyRegistry(EnvKeyRegistry("envkey:read"), CachingKeyRegistry(store))

        key = await registry.get(TOKEN)
        assert key is not None and key.name == "from-db"

    async def test_unknown_token_resolves_to_none(self, clock):
        registry = ChainedKeyRegistry(EnvKeyRegistry("envkey:read"), CachingKeyRegistry(FakeStore()))
        assert await registry.get(OTHER) is None

    async def test_env_keys_still_work_if_the_store_is_ordered_first(self, clock):
        """Ordering is a performance choice, not a correctness one.

        Putting the database first must not stop env keys — the statically stable
        floor — from resolving while the database is down.
        """
        store = FakeStore()
        store.broken = True
        registry = ChainedKeyRegistry(CachingKeyRegistry(store), EnvKeyRegistry("envkey:read"))

        key = await registry.get("envkey")
        assert key is not None and key.read

    async def test_unknown_token_during_an_outage_reports_the_outage(self, clock):
        # Not 401: no layer could confirm the token is bad, so don't claim it is.
        store = FakeStore()
        store.broken = True
        registry = ChainedKeyRegistry(EnvKeyRegistry("envkey:read"), CachingKeyRegistry(store))

        with pytest.raises(KeyStoreUnavailable):
            await registry.get(OTHER)
