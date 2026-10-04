from typing import Protocol, Annotated
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import Security, WebSocket, status
from fastapi.security import APIKeyHeader
from starlette.types import Scope
from fastapi.exceptions import WebSocketException

from canopy.core.config import Settings, get_settings
from canopy.core.errors import (
    ErrorCode,
    InvalidAPIKey,
    InsufficientReadAccess,
    InsufficientWriteAccess,
)


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
    """

    def __init__(self, raw: str = "") -> None:
        self._keys: dict[str, APIKey] = {}
        for entry in (e.strip() for e in raw.split(",") if e.strip()):
            try:
                token, scopes = entry.split(":", 1)
            except ValueError:
                continue
            token = token.strip()
            scope_set = {s.strip() for s in scopes.split("+")}
            self._keys[token] = APIKey(
                token=token,
                read="read" in scope_set,
                write="write" in scope_set,
            )

    def get(self, token: str) -> APIKey | None:
        return self._keys.get(token)

    def __len__(self) -> int:
        return len(self._keys)


class KeysFileError(RuntimeError):
    """The configured keys file cannot supply a registry. Never carries file content."""


def load_keys_file(path: str | Path) -> EnvKeyRegistry:
    """Build the registry from a keys file (the ``CANOPY_API_KEYS`` grammar).

    Entries are separated by commas or line breaks. Unlike the environment
    value, the file is read strictly: one that cannot be read, holds no key, or
    holds an entry that is not ``<token>:<scope>[+<scope>]`` with scopes
    ``read`` / ``write`` raises ``KeysFileError``. A deployment that names a
    file must never run on a registry other than the one the file spells out.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: cannot be read ({reason})") from None
    entries = [entry.strip() for entry in raw.replace("\r", "\n").replace("\n", ",").split(",") if entry.strip()]
    if not entries:
        raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: holds no API key")
    for number, entry in enumerate(entries, start=1):
        token, separator, scopes = entry.partition(":")
        well_formed = (
            bool(separator)
            and bool(token)
            and not any(char.isspace() for char in entry)
            and all(scope in ("read", "write") for scope in scopes.split("+"))
        )
        if not well_formed:
            raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: entry {number} is not <token>:<scope>[+<scope>]")
    return EnvKeyRegistry(",".join(entries))


def build_key_registry(settings: Settings) -> KeyRegistry:
    """The registry these settings describe: the keys file when one is set, else ``api_keys``."""
    if settings.api_keys_file:
        return load_keys_file(settings.api_keys_file)
    return EnvKeyRegistry(settings.api_keys)


_registry: KeyRegistry | None = None


def bind_key_registry(registry: KeyRegistry) -> None:
    """Wire a registry. Called once at app startup; tests can re-bind freely."""
    global _registry
    _registry = registry


def load_key_registry() -> KeyRegistry:
    """Build the registry from the current settings and bind it.

    Called while the app is composed, so a bad keys file stops the process
    before it serves anything instead of surfacing on the first request.
    """
    registry = build_key_registry(get_settings())
    bind_key_registry(registry)
    return registry


def _get_registry() -> KeyRegistry:
    if _registry is None:
        return load_key_registry()
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


def key_from_scope(scope: Scope) -> APIKey | None:
    """Resolve the API key carried by a raw ASGI scope, or ``None``.

    For code that runs outside dependency injection. Reads what the route
    dependencies read: the ``X-API-Key`` header, and for a WebSocket also the
    ``api_key`` query parameter (browsers cannot set headers on a handshake).
    """
    token = next(
        (value.decode("latin-1") for name, value in scope.get("headers", ()) if name.lower() == b"x-api-key"),
        None,
    )
    if not token and scope["type"] == "websocket":
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = next(iter(query.get("api_key", ())), None)
    if not token:
        return None
    return _get_registry().get(token)


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
