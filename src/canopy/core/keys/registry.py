"""`KeyRegistry` implementations for persistent keys.

Three small pieces, composed in ``canopy.main``'s lifespan:

    ChainedKeyRegistry(
        EnvKeyRegistry(settings.api_keys),          # statically stable floor
        CachingKeyRegistry(DbKeyRegistry()),        # revocable, DB-backed
    )

``DbKeyRegistry`` only knows how to look a token up. ``CachingKeyRegistry`` owns
the TTL, the negative cache, and the serve-stale behaviour, so that policy is
testable without a database and reusable over any inner registry.
"""

from __future__ import annotations

import logging
from time import monotonic
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from canopy.core.db import session_scope
from canopy.core.errors import KeyStoreUnavailable
from canopy.core.security import APIKey, KeyRegistry
from canopy.core.keys.models import ApiKey
from canopy.core.keys.tokens import hash_token
from canopy.core.keys.repositories import ApiKeyRepository

logger = logging.getLogger(__name__)

# What "the store is unreachable" actually looks like. asyncpg surfaces a refused
# connection as a bare OSError raised by asyncio's `create_connection`, which
# SQLAlchemy never wraps — catching only SQLAlchemyError lets a real outage escape
# as a 500 with a traceback instead of taking the serve-stale path. TimeoutError
# subclasses OSError, so connect timeouts are covered too. Deliberately not
# `Exception`: a bug in this module must not be reported as a database outage.
STORE_ERRORS = (SQLAlchemyError, OSError)


def _to_api_key(row: ApiKey, token: str) -> APIKey:
    return APIKey(
        token=token,
        read=row.read_access,
        write=row.write_access,
        admin=row.admin_access,
        name=row.app_name,
        id=row.id,
    )


class DbKeyRegistry:
    """Resolves a token against the ``core.api_key`` table.

    Opens its own short-lived session rather than taking the request-scoped one:
    this runs inside a `Security` dependency on every route, and must not hold a
    transaction open for the life of a request.

    Also stamps ``last_used_at`` on the way through. That write is naturally
    throttled by the caching layer in front — a given token only reaches here once
    per cache TTL — so it needs no throttling machinery of its own.
    """

    async def get(self, token: str) -> APIKey | None:
        token_hash = hash_token(token)
        async with session_scope() as session:
            repo = ApiKeyRepository(session)
            row = await repo.get_by_token_hash(token_hash, active_only=True)
            if row is None:
                return None
            key = _to_api_key(row, token)
            try:
                await repo.touch_last_used(row.id)
                await session.commit()
            except STORE_ERRORS:
                # Attribution is a nicety; never fail an otherwise valid request for it.
                logger.warning("Could not stamp last_used_at for API key id=%s", row.id)
                await session.rollback()
            return key


@dataclass(frozen=True)
class _CacheEntry:
    key: APIKey | None  # None is a cached negative — "no such active key"
    fetched_at: float  # monotonic seconds


class CachingKeyRegistry:
    """TTL cache in front of another registry, with bounded serve-stale.

    Three behaviours worth stating outright, because they are the auth contract:

    * **Revocation latency equals ``ttl_s``.** A key deactivated in the database
      keeps working until its cached entry expires.
    * **The store going down does not take auth down.** If the inner registry
      raises, a cached key is served up to ``max_stale_s`` past its fetch. Canopy
      has ten mounted sub-apps of which seven need no database at all; a Postgres
      outage must not black out live PV streaming. Past ``max_stale_s`` the entry
      is refused, so a revoked key cannot survive an arbitrarily long outage.
    * **Negatives are never served stale.** An unknown token during an outage
      fails closed, with a distinguishable 503 rather than a misleading 401.
    """

    def __init__(
        self,
        inner: KeyRegistry,
        ttl_s: float = 60.0,
        negative_ttl_s: float = 5.0,
        max_stale_s: float = 3600.0,
        max_entries: int = 10_000,
    ) -> None:
        self._inner = inner
        self._ttl_s = ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._max_stale_s = max_stale_s
        self._max_entries = max_entries
        self._cache: dict[str, _CacheEntry] = {}

    async def get(self, token: str) -> APIKey | None:
        # Keyed by hash so a long-lived process dict never holds plaintext tokens.
        cache_key = hash_token(token)
        now = monotonic()

        entry = self._cache.get(cache_key)
        if entry is not None:
            ttl = self._ttl_s if entry.key is not None else self._negative_ttl_s
            if now - entry.fetched_at < ttl:
                return entry.key

        try:
            key = await self._inner.get(token)
        except STORE_ERRORS as exc:
            return self._serve_stale(entry, now, exc)

        self._store(cache_key, key, now)
        return key

    def _serve_stale(self, entry: _CacheEntry | None, now: float, exc: Exception) -> APIKey:
        if entry is not None and entry.key is not None and now - entry.fetched_at <= self._max_stale_s:
            logger.warning(
                "API key store unavailable; serving cached key for app_name=%s (%.0fs stale)",
                entry.key.name,
                now - entry.fetched_at,
            )
            return entry.key
        logger.error("API key store unavailable with no usable cached entry: %s", exc)
        raise KeyStoreUnavailable("API key store is unavailable") from exc

    def _store(self, cache_key: str, key: APIKey | None, now: float) -> None:
        # Positives are bounded by the number of real keys, so they are always
        # cached. Negatives are attacker-controlled: a flood of invented tokens
        # would otherwise grow this dict without limit, so once the cache is full
        # they go uncached and simply pay the lookup each time.
        if key is None and len(self._cache) >= self._max_entries:
            self._prune(now)
            if len(self._cache) >= self._max_entries:
                return
        self._cache[cache_key] = _CacheEntry(key=key, fetched_at=now)

    def _prune(self, now: float) -> None:
        """Drop entries that can no longer be used for anything — negatives past
        their TTL, positives past the stale horizon."""
        for cache_key, entry in list(self._cache.items()):
            horizon = self._max_stale_s if entry.key is not None else self._negative_ttl_s
            if now - entry.fetched_at > horizon:
                del self._cache[cache_key]

    def invalidate(self, token: str) -> None:
        """Drop a token's cached entry so a revocation takes effect immediately in
        this process. Used by the CLI/API path after a deactivate."""
        self._cache.pop(hash_token(token), None)


class ChainedKeyRegistry:
    """Try each registry in order; first match wins.

    Env keys are checked before the database so the highest-volume credential in the
    system — the read key baked into every display — never makes a database round
    trip at all, and is unaffected by database latency as well as outages.

    An unavailable layer does not end the search: it is remembered and the remaining
    layers are still tried, so a layer that *can* answer still does. Only if nothing
    matched and something was down does the outage surface — reporting "invalid key"
    for a token that could not actually be checked would send an operator hunting for
    the wrong problem. This keeps the ordering a performance decision rather than a
    correctness one: reordering the chain cannot silently take auth down during an
    outage.
    """

    def __init__(self, *registries: KeyRegistry) -> None:
        self._registries = registries

    async def get(self, token: str) -> APIKey | None:
        unavailable: KeyStoreUnavailable | None = None
        for registry in self._registries:
            try:
                key = await registry.get(token)
            except KeyStoreUnavailable as exc:
                unavailable = exc
                continue
            if key is not None:
                return key
        if unavailable is not None:
            raise unavailable
        return None
