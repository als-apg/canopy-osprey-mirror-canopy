import sys
import logging
import importlib.util
from typing import Protocol, Annotated
from pathlib import Path
from dataclasses import field, dataclass
from urllib.parse import parse_qs

from fastapi import Request, Security, WebSocket, status
from starlette.types import Scope
from fastapi.security import APIKeyHeader
from fastapi.exceptions import WebSocketException

from canopy.core.config import Settings, get_settings
from canopy.core.errors import (
    ErrorCode,
    InvalidAPIKey,
    KeyStoreUnavailable,
    InsufficientReadAccess,
    InsufficientAdminAccess,
    InsufficientWriteAccess,
    InsufficientModuleAccess,
)
from canopy.core.modules import JOBS, WILDCARD, split_modules, module_for_app

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class APIKey:
    """A resolved key. `name` identifies the holding client class — it is what
    request logs and analytics may record; the `token` never is.

    `token` is only populated by ``EnvKeyRegistry`` and ``load_keys_file``; ``DbKeyRegistry`` leaves it
    empty since nothing downstream reads it back. Excluded from `repr` regardless,
    so a stray ``logger.debug("%s", key)`` can never print it.

    `modules` has no default: every real registry (``EnvKeyRegistry``,
    ``DbKeyRegistry``) already passes it explicitly, so a future one that
    forgets to must fail loudly rather than silently granting every module.
    """

    read: bool
    write: bool
    modules: frozenset[str]
    admin: bool = False
    name: str = "unknown"
    id: str | None = None
    token: str = field(default="", repr=False)

    def allows_module(self, module: str) -> bool:
        return module == JOBS or WILDCARD in self.modules or module in self.modules


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

    Format: ``"<token>:<scope>[+<scope>]*[@<module>[+<module>]*][,...]"``.
    Scopes are ``read``, ``write`` and ``admin``. The ``@<module>...`` suffix
    restricts the key to those modules; pass ``@*`` for every module. An entry
    with no ``@`` at all is deprecated but not fatal — it defaults to ``@*``
    with a warning, so an env var written before module scoping existed keeps
    granting the same access it always did. Examples:

        CANOPY_API_KEYS=""                              # empty registry
        CANOPY_API_KEYS="dev:read+write@*"              # one key, full access
        CANOPY_API_KEYS="ro:read@*,rw:read+write@*"     # two keys
        CANOPY_API_KEYS="ops:admin@*"                   # key management only
        CANOPY_API_KEYS="ops:admin@core"                # admin, core routes only
        CANOPY_API_KEYS="fox:read+write@squirrel+fox"   # scoped to two modules

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
                token, rest = entry.split(":", 1)
            except ValueError:
                logger.warning(
                    "Skipping malformed CANOPY_API_KEYS entry #%d: expected '<token>:<scope>[+<scope>]'", position
                )
                continue
            token = token.strip()
            if not token:
                logger.warning("Skipping CANOPY_API_KEYS entry #%d: empty token", position)
                continue
            scopes, has_at, modules_part = rest.partition("@")
            scope_set = {s.strip() for s in scopes.split("+")}
            if unknown := scope_set - {"read", "write", "admin"}:
                logger.warning(
                    "Skipping CANOPY_API_KEYS entry #%d: %d unrecognized scope(s); "
                    "only 'read', 'write' and 'admin' are valid",
                    position,
                    len(unknown),
                )
                continue
            if not has_at:
                logger.warning(
                    "CANOPY_API_KEYS entry #%d has no '@<module>' suffix; defaulting to '@*' (every module). "
                    "This default is deprecated — add '@*' explicitly, or scope the key, before it is removed.",
                    position,
                )
                modules_part = WILDCARD
            module_set = {m.strip() for m in modules_part.split("+") if m.strip()}
            if not module_set:
                logger.warning("Skipping CANOPY_API_KEYS entry #%d: empty '@' module list", position)
                continue
            known_modules, unknown_modules = split_modules(module_set)
            if unknown_modules:
                logger.warning(
                    "CANOPY_API_KEYS entry #%d has %d unrecognized module(s); ignoring them",
                    position,
                    len(unknown_modules),
                )
            if not known_modules:
                logger.warning(
                    "Skipping CANOPY_API_KEYS entry #%d: no recognized modules remained after filtering", position
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
                modules=known_modules,
            )
        if self._keys:
            logger.info(
                "Registered %d API key(s) from CANOPY_API_KEYS (%d read, %d write, %d admin, %d module-scoped)",
                len(self._keys),
                sum(1 for key in self._keys.values() if key.read),
                sum(1 for key in self._keys.values() if key.write),
                sum(1 for key in self._keys.values() if key.admin),
                sum(1 for key in self._keys.values() if WILDCARD not in key.modules),
            )
        else:
            logger.info("No API keys registered from CANOPY_API_KEYS; relying on the database registry")

    async def get(self, token: str) -> APIKey | None:
        return self._keys.get(token)

    def __len__(self) -> int:
        return len(self._keys)


class KeysFileError(RuntimeError):
    """The configured keys file cannot supply a registry. Never carries file content."""


KEYS_FILE_SCOPES = frozenset({"read", "write", "admin"})


class FileKeyRegistry:
    """In-memory registry holding exactly the keys of a keys file.

    Built only by ``load_keys_file``, which has already refused anything the
    file does not spell out in full; nothing here parses or defaults.
    """

    def __init__(self, keys: dict[str, APIKey]) -> None:
        self._keys = dict(keys)

    async def get(self, token: str) -> APIKey | None:
        return self._keys.get(token)

    def __len__(self) -> int:
        return len(self._keys)


def _file_entry_problem(entry: str) -> str | None:
    """Why ``entry`` is not a keys-file line, or ``None`` when it is one.

    The reason names the rule broken, never a part of the entry: the token is
    the secret, and a scope or module typo sits right next to it.
    """
    if any(char.isspace() for char in entry):
        return "contains whitespace"
    token, separator, rest = entry.partition(":")
    if not separator or not token:
        return "is not <token>:<scope>[+<scope>]@<module>[+<module>]"
    scopes, at, modules = rest.partition("@")
    if not at:
        return "has no @<module>[+<module>] suffix"
    scope_list = scopes.split("+")
    if not all(scope_list) or not set(scope_list) <= KEYS_FILE_SCOPES:
        return "names a scope other than read, write or admin"
    if "read" not in scope_list:
        return "grants no read scope"
    module_list = modules.split("+")
    if not all(module_list):
        return "has an empty module in its @<module>[+<module>] suffix"
    if split_modules(set(module_list))[1]:
        return "names an unknown module"
    return None


def load_keys_file(path: str | Path) -> FileKeyRegistry:
    """Build the registry from a keys file.

    Each entry is ``<token>:<scope>[+<scope>]@<module>[+<module>]``: scopes
    ``read``, ``write`` and ``admin``, ``read`` always among them, and modules
    from ``canopy.core.modules`` (``*`` for every module). Entries are separated
    by commas or line breaks. Unlike ``CANOPY_API_KEYS`` the file is read
    strictly: a file that cannot be read, holds no key, or holds one entry that
    breaks the grammar raises ``KeysFileError`` naming the entry's position, and
    there is no default module scope. A deployment that names a file must never
    run on a registry other than the one the file spells out.

    Imports nothing from ``canopy.core.keys``: a keys file is the whole registry,
    with no database tier behind it.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: cannot be read ({reason})") from None
    entries = [entry.strip() for entry in raw.replace("\r", "\n").replace("\n", ",").split(",") if entry.strip()]
    if not entries:
        raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: holds no API key")
    keys: dict[str, APIKey] = {}
    for number, entry in enumerate(entries, start=1):
        if problem := _file_entry_problem(entry):
            raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: entry {number} {problem}")
        token, _, rest = entry.partition(":")
        scopes, _, modules = rest.partition("@")
        if token in keys:
            raise KeysFileError(f"CANOPY_API_KEYS_FILE {path}: entry {number} repeats an earlier token")
        scope_set = set(scopes.split("+"))
        keys[token] = APIKey(
            read=True,
            write="write" in scope_set,
            admin="admin" in scope_set,
            modules=split_modules(set(modules.split("+")))[0],
            # Position identifies a file key in logs without revealing its token.
            name=f"file:{number}",
            token=token,
        )
    logger.info(
        "Registered %d API key(s) from CANOPY_API_KEYS_FILE (%d write, %d admin, %d module-scoped)",
        len(keys),
        sum(1 for key in keys.values() if key.write),
        sum(1 for key in keys.values() if key.admin),
        sum(1 for key in keys.values() if WILDCARD not in key.modules),
    )
    return FileKeyRegistry(keys)


def build_key_registry(settings: Settings) -> KeyRegistry:
    """The registry these settings describe.

    A keys file, when one is set, is the whole registry: no env keys, no
    database tier, and nothing imported from ``canopy.core.keys``. Otherwise the
    env keys sit in front of the database chain of ``canopy.core.keys.registry``
    when SQLAlchemy is installed, and stand alone when it is not: an install
    without the ``data`` extra must still be able to import ``canopy.main``.
    """
    if settings.api_keys_file:
        return load_keys_file(settings.api_keys_file)
    if importlib.util.find_spec("sqlalchemy") is not None:
        # Imported here: the database chain must never load for a keys file or a lean install.
        from canopy.core.keys.registry import build_key_registry as build_database_chain

        return build_database_chain(settings)
    return EnvKeyRegistry(settings.api_keys)


def is_database_chain(registry: KeyRegistry) -> bool:
    """True when ``registry`` is the env + database chain, which serves ``/v1/api-keys``."""
    chain = sys.modules.get("canopy.core.keys.registry")
    return chain is not None and isinstance(registry, chain.ChainedKeyRegistry)


_registry: KeyRegistry | None = None


def bind_key_registry(registry: KeyRegistry) -> None:
    """Wire a registry directly. Tests use this to install a fake; app startup uses `get_registry` instead."""
    global _registry
    _registry = registry


def load_key_registry() -> KeyRegistry:
    """Build the registry from the current settings and bind it.

    Called once while the app is composed, so a bad keys file stops the process
    before it serves anything instead of surfacing on the first request.
    """
    registry = build_key_registry(get_settings())
    bind_key_registry(registry)
    return registry


def get_registry() -> KeyRegistry:
    """Return the bound registry, building it from settings on first use.

    A sub-app served without ``canopy.main.build_app`` having run (a standalone
    mount in a test) still gets the registry the settings describe.
    """
    if _registry is None:
        return load_key_registry()
    return _registry


def invalidate_key_id(key_id: str) -> None:
    """Drop a deactivated key's cached entry, if the bound registry caches at all,
    so revocation is immediate rather than waiting out the cache TTL. A no-op for
    registries that don't cache (an `EnvKeyRegistry` alone, or a fake in tests).
    """
    invalidate = getattr(get_registry(), "invalidate_key_id", None)
    if invalidate is not None:
        invalidate(key_id)


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_api_key(request: Request, token: Annotated[str | None, Security(api_key_header)]) -> APIKey:
    if not token:
        raise InvalidAPIKey("Missing X-API-Key header")
    key = await get_registry().get(token)
    if key is None:
        raise InvalidAPIKey("Invalid API key")
    module = module_for_app(request.app)
    if not key.allows_module(module):
        raise InsufficientModuleAccess(f"API key is not scoped to '{module}'")
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
    module = module_for_app(websocket.app)
    if not key.allows_module(module):
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"{ErrorCode.INSUFFICIENT_MODULE_ACCESS.value}: API key is not scoped to '{module}'",
        )
    return key


def token_from_scope(scope: Scope) -> str | None:
    """The API key token carried by a raw ASGI scope, or ``None``.

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
    return token or None


async def key_from_scope(scope: Scope) -> APIKey | None:
    """Resolve the API key carried by a raw ASGI scope, or ``None``.

    Raises ``KeyStoreUnavailable`` when the registry cannot answer.
    """
    token = token_from_scope(scope)
    if token is None:
        return None
    return await get_registry().get(token)


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
