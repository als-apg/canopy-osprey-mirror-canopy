import logging
from typing import Protocol, Annotated
from dataclasses import dataclass

from fastapi import Security, WebSocket, status
from fastapi.security import APIKeyHeader
from fastapi.exceptions import WebSocketException

from canopy.core.config import get_settings
from canopy.core.errors import (
    ErrorCode,
    InvalidAPIKey,
    KeyStoreUnavailable,
    InsufficientReadAccess,
    InsufficientAdminAccess,
    InsufficientWriteAccess,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class APIKey:
    """A resolved key. `name` identifies the holding client class — it is what
    request logs and analytics may record; the `token` never is."""

    token: str
    read: bool
    write: bool
    admin: bool = False
    name: str = "unknown"
    id: str | None = None


class KeyRegistry(Protocol):
    """Strategy for resolving an API key token to an `APIKey` record (or None).

    `EnvKeyRegistry` resolves from the environment; the registries in
    `canopy.core.keys.registry` resolve from Postgres and layer caching on top.
    Routes and dependencies are identical either way.

    `get` is async because a persistent implementation has to await the database;
    an in-memory one simply doesn't await anything.
    """

    async def get(self, token: str) -> APIKey | None: ...


class EnvKeyRegistry:
    """In-memory registry built from a comma-separated env-var entry list.

    Format: ``"<token>:<scope>[+<scope>]*[,<token>:<scope>...]"``. Scopes are
    ``read``, ``write`` and ``admin``. Examples:

        CANOPY_API_KEYS=""                              # empty registry
        CANOPY_API_KEYS="dev:read+write"                # one key, full access
        CANOPY_API_KEYS="ro:read,rw:read+write"         # two keys
        CANOPY_API_KEYS="ops:admin"                     # key management only

    Env keys are no longer a stop-gap: they are the statically stable layer in
    front of the Postgres-backed registry. Keys defined here keep working when the
    database is unreachable — including on a cold start, when there is nothing
    cached to fall back on — so a deployment keeps a small, long-lived set here
    (a read key for displays, an admin key for ops) and manages everything else in
    the database, where revocation is immediate.

    A typo'd entry is skipped rather than fatal, so parsing logs a warning for
    each one it drops — an unreported skip means a deployed instance rejects a
    client for a reason nothing on the box explains. Entries are identified by
    position, never by content: the token is the secret.
    """

    def __init__(self, raw: str = "") -> None:
        self._keys: dict[str, APIKey] = {}
        for position, entry in enumerate((e.strip() for e in raw.split(",")), start=1):
            if not entry:
                continue
            try:
                token, scopes = entry.split(":", 1)
            except ValueError:
                logger.warning(
                    "Skipping malformed CANOPY_API_KEYS entry #%d: expected '<token>:<scope>[+<scope>]'", position
                )
                continue
            token = token.strip()
            if not token:
                logger.warning("Skipping CANOPY_API_KEYS entry #%d: empty token", position)
                continue
            scope_set = {s.strip() for s in scopes.split("+")}
            if unknown := scope_set - {"read", "write", "admin"}:
                logger.warning(
                    "Skipping CANOPY_API_KEYS entry #%d: unrecognized scope(s) %s; "
                    "only 'read', 'write' and 'admin' are valid",
                    position,
                    sorted(unknown),
                )
                continue
            if token in self._keys:
                logger.warning("CANOPY_API_KEYS entry #%d duplicates an earlier token; the later entry wins", position)
            self._keys[token] = APIKey(
                token=token,
                read="read" in scope_set,
                write="write" in scope_set,
                admin="admin" in scope_set,
                # Env keys have no app name of their own; position identifies them in
                # logs without ever revealing the token.
                name=f"env:{position}",
            )
        if self._keys:
            logger.info(
                "Registered %d API key(s) from CANOPY_API_KEYS (%d read, %d write, %d admin)",
                len(self._keys),
                sum(1 for key in self._keys.values() if key.read),
                sum(1 for key in self._keys.values() if key.write),
                sum(1 for key in self._keys.values() if key.admin),
            )
        else:
            logger.info("No API keys registered from CANOPY_API_KEYS; relying on the database registry")

    async def get(self, token: str) -> APIKey | None:
        return self._keys.get(token)


_registry: KeyRegistry | None = None


def bind_key_registry(registry: KeyRegistry) -> None:
    """Wire a registry directly. Tests use this to install a fake; app startup uses `get_registry` instead."""
    global _registry
    _registry = registry


def get_registry() -> KeyRegistry:
    """Return the bound registry, building the default from settings on first use."""
    global _registry
    if _registry is None:
        _registry = EnvKeyRegistry(get_settings().api_keys)
    return _registry


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_api_key(token: Annotated[str | None, Security(api_key_header)]) -> APIKey:
    if not token:
        raise InvalidAPIKey("Missing X-API-Key header")
    key = await get_registry().get(token)
    if key is None:
        raise InvalidAPIKey("Invalid API key")
    return key


APIKeyDep = Annotated[APIKey, Security(get_api_key)]


async def get_optional_api_key(token: Annotated[str | None, Security(api_key_header)]) -> APIKey | None:
    """Resolve the API key if one was sent and is valid; otherwise ``None``.

    Never raises: for endpoints (analytics ingestion) that accept anonymous
    requests but attach key metadata when available. An *invalid* token also
    resolves to ``None`` rather than 401 so a stale key can't break a client's
    fire-and-forget calls — including when the token is merely unrecognized by
    the env layer and the database registry behind it is unreachable.
    """
    if not token:
        return None
    try:
        return await get_registry().get(token)
    except KeyStoreUnavailable:
        return None


OptionalAPIKeyDep = Annotated[APIKey | None, Security(get_optional_api_key)]


def require_read_access(key: APIKeyDep) -> APIKey:
    if not key.read:
        raise InsufficientReadAccess("API key does not have read access")
    return key


def require_write_access(key: APIKeyDep) -> APIKey:
    if not key.write:
        raise InsufficientWriteAccess("API key does not have write access")
    return key


def require_admin_access(key: APIKeyDep) -> APIKey:
    """Gate for key management and other operational (not data-plane) actions.

    Orthogonal to ``read``/``write``, not a level above them — an admin key that
    cannot itself read a PV is the better credential for a provisioning tool.
    """
    if not key.admin:
        raise InsufficientAdminAccess("API key does not have admin access")
    return key


AdminKeyDep = Annotated[APIKey, Security(require_admin_access)]

# No WebSocket variant of require_admin_access: nothing about key management streams.

# WebSocket variants raise WebSocketException because handshake errors must close the connection, not return JSON.


async def ws_get_api_key(websocket: WebSocket) -> APIKey:
    token = websocket.headers.get("X-API-Key") or websocket.query_params.get("api_key")
    if not token:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INVALID_API_KEY.value}: missing X-API-Key",
        )
    try:
        key = await get_registry().get(token)
    except KeyStoreUnavailable:
        # A handshake can't return JSON, so translate to a close reason that names
        # the real cause: the store is down, the token isn't necessarily bad.
        raise WebSocketException(
            code=status.WS_1011_INTERNAL_ERROR,
            reason=f"{ErrorCode.KEY_STORE_UNAVAILABLE.value}: API key store unavailable",
        )
    if key is None:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INVALID_API_KEY.value}: invalid API key",
        )
    return key


def ws_require_read_access(key: Annotated[APIKey, Security(ws_get_api_key)]) -> APIKey:
    if not key.read:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INSUFFICIENT_READ_ACCESS.value}: no read access",
        )
    return key


def ws_require_write_access(key: Annotated[APIKey, Security(ws_get_api_key)]) -> APIKey:
    if not key.write:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INSUFFICIENT_WRITE_ACCESS.value}: no write access",
        )
    return key
