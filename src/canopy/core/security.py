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
    InsufficientReadAccess,
    InsufficientWriteAccess,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class APIKey:
    token: str
    read: bool
    write: bool


class KeyRegistry(Protocol):
    """Strategy for resolving an API key token to an `APIKey` record (or None).

    `EnvKeyRegistry` is the v1 implementation. A future Postgres-backed
    registry will implement the same Protocol; routes and dependencies do
    not change.
    """

    def get(self, token: str) -> APIKey | None: ...


class EnvKeyRegistry:
    """In-memory registry built from a comma-separated env-var entry list.

    Format: ``"<token>:<scope>[+<scope>]*[,<token>:<scope>...]"``. Scopes are
    ``read`` and ``write``. Examples:

        CANOPY_API_KEYS=""                              # empty registry
        CANOPY_API_KEYS="dev:read+write"                # one key, full access
        CANOPY_API_KEYS="ro:read,rw:read+write"         # two keys

    Stop-gap until a persistent module brings up Postgres-backed keys.

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
            if unknown := scope_set - {"read", "write"}:
                logger.warning(
                    "Skipping CANOPY_API_KEYS entry #%d: %d unrecognized scope(s); only 'read' and 'write' are valid",
                    position,
                    len(unknown),
                )
                continue
            if token in self._keys:
                logger.warning("CANOPY_API_KEYS entry #%d duplicates an earlier token; the later entry wins", position)
            self._keys[token] = APIKey(
                token=token,
                read="read" in scope_set,
                write="write" in scope_set,
            )
        if self._keys:
            logger.info(
                "Registered %d API key(s) from CANOPY_API_KEYS (%d read, %d write)",
                len(self._keys),
                sum(1 for key in self._keys.values() if key.read),
                sum(1 for key in self._keys.values() if key.write),
            )
        else:
            logger.warning("No API keys registered from CANOPY_API_KEYS: every gated endpoint will return 401")

    def get(self, token: str) -> APIKey | None:
        return self._keys.get(token)


_registry: KeyRegistry | None = None


def bind_key_registry(registry: KeyRegistry) -> None:
    """Wire a registry. Called once at app startup; tests can re-bind freely."""
    global _registry
    _registry = registry


def _get_registry() -> KeyRegistry:
    global _registry
    if _registry is None:
        _registry = EnvKeyRegistry(get_settings().api_keys)
    return _registry


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_api_key(token: Annotated[str | None, Security(api_key_header)]) -> APIKey:
    if not token:
        raise InvalidAPIKey("Missing X-API-Key header")
    key = _get_registry().get(token)
    if key is None:
        raise InvalidAPIKey("Invalid API key")
    return key


APIKeyDep = Annotated[APIKey, Security(get_api_key)]


def get_optional_api_key(token: Annotated[str | None, Security(api_key_header)]) -> APIKey | None:
    """Resolve the API key if one was sent and is valid; otherwise ``None``.

    Never raises: for endpoints (analytics ingestion) that accept anonymous
    requests but attach key metadata when available. An *invalid* token also
    resolves to ``None`` rather than 401 so a stale key can't break a client's
    fire-and-forget calls.
    """
    if not token:
        return None
    return _get_registry().get(token)


OptionalAPIKeyDep = Annotated[APIKey | None, Security(get_optional_api_key)]


def require_read_access(key: APIKeyDep) -> APIKey:
    if not key.read:
        raise InsufficientReadAccess("API key does not have read access")
    return key


def require_write_access(key: APIKeyDep) -> APIKey:
    if not key.write:
        raise InsufficientWriteAccess("API key does not have write access")
    return key


# WebSocket variants raise WebSocketException because handshake errors must close the connection, not return JSON.


async def ws_get_api_key(websocket: WebSocket) -> APIKey:
    token = websocket.headers.get("X-API-Key") or websocket.query_params.get("api_key")
    if not token:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INVALID_API_KEY.value}: missing X-API-Key",
        )
    key = _get_registry().get(token)
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
