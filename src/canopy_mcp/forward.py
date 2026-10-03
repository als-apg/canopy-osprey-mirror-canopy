"""The read-only forward of the CANOPY API: two WebSocket relays and the Mole reads.

The served viewer reads live values over ``WS /hare/v1/ws/pvs`` and
``WS /fox/v1/ws/formulas``. Both are relayed to ``CANOPY_BASE_URL`` with the
server's own ``X-API-Key`` (``CANOPY_API_KEY``) on the upstream handshake. The
upstream handshake is built from scratch -- no client header is copied and any
``api_key`` query parameter is dropped -- so a browser can neither see the key
nor substitute its own.

``GET /mole/v1/{path}`` (the Trace tab's archiver reads) is forwarded the same
way over HTTP: only ``Accept`` and the server key go upstream, ``api_key`` is
dropped, and a path that would leave ``/mole/v1/`` once decoded is a 404.

Every other method and path under the backend's service prefixes answers 405,
in particular ``PUT /hare/v1/pvs/*`` (a PV write) and
``POST /squirrel/*/restore``: this process fronts the backend read-only.
WebSocket connections to any other path fall through to Starlette's default,
which refuses the handshake.
"""

import posixpath
from urllib.parse import quote, unquote, urlsplit, urlencode, urlunsplit, unquote_plus
from collections.abc import Callable, Iterable, Awaitable

import anyio
import httpx
from websockets import ConnectionClosed, InvalidHandshake
from starlette.routing import Route, BaseRoute, WebSocketRoute
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, PlainTextResponse
from starlette.websockets import WebSocket, WebSocketState
from websockets.asyncio.client import ClientConnection, connect

from canopy.core.errors import ErrorCode
from canopy_mcp.client import auth_headers
from canopy_mcp.config import Settings

RELAYED_PATHS = ("/hare/v1/ws/pvs", "/fox/v1/ws/formulas")
SERVICE_PREFIXES = ("hare", "fox", "squirrel", "beaver", "raccoon", "mole")
MOLE_PREFIX = "/mole/v1/"
MOLE_TIMEOUT = 15.0
API_KEY_QUERY = "api_key"
OPEN_TIMEOUT = 10.0

# Close codes that describe a closure but may not be sent in a close frame.
_UNSENDABLE_CLOSE_CODES = {1005, 1006, 1015}


def upstream_ws_url(base_url: str, path: str, query: Iterable[tuple[str, str]]) -> str:
    """The upstream WebSocket URL for ``path``, without any ``api_key`` parameter."""
    base = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(base.scheme, base.scheme)
    kept = [(name, value) for name, value in query if name.lower() != API_KEY_QUERY]
    return urlunsplit((scheme, base.netloc, base.path.rstrip("/") + path, urlencode(kept), ""))


def _sendable(code: int | None, default: int) -> int:
    return default if code is None or code in _UNSENDABLE_CLOSE_CODES else code


async def _pipe(websocket: WebSocket, upstream: ClientConnection) -> None:
    """Relay frames both ways until either side closes, then close the other."""

    async with anyio.create_task_group() as tg:

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    await upstream.close(_sendable(message.get("code"), 1000))
                    break
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
            tg.cancel_scope.cancel()

        async def upstream_to_client() -> None:
            try:
                async for frame in upstream:
                    if isinstance(frame, str):
                        await websocket.send_text(frame)
                    else:
                        await websocket.send_bytes(frame)
            except ConnectionClosed:
                pass
            if websocket.application_state is WebSocketState.CONNECTED:
                code = _sendable(upstream.close_code, 1011 if upstream.close_code == 1006 else 1000)
                await websocket.close(code=code, reason=upstream.close_reason or "")
            tg.cancel_scope.cancel()

        tg.start_soon(client_to_upstream)
        tg.start_soon(upstream_to_client)


def make_relay(settings: Settings, path: str) -> Callable[[WebSocket], Awaitable[None]]:
    """The WebSocket endpoint relaying ``path`` to the backend."""
    headers = auth_headers(settings)

    async def relay(websocket: WebSocket) -> None:
        url = upstream_ws_url(settings.base_url, path, websocket.query_params.multi_items())
        try:
            # compression=None: the in-cluster hop never negotiates permessage-deflate.
            upstream = await connect(
                url,
                additional_headers=headers,
                open_timeout=OPEN_TIMEOUT,
                max_size=None,
                user_agent_header=None,
                compression=None,
            )
        except (OSError, TimeoutError, InvalidHandshake):
            # Closing before accept refuses the client's handshake.
            await websocket.close(code=1011)
            return
        async with upstream:
            await websocket.accept()
            await _pipe(websocket, upstream)

    return relay


def _fully_decoded(path: str) -> str:
    """``path`` percent-decoded until stable, so ``%252e`` cannot hide a ``..``."""
    for _ in range(8):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    return path


def mole_upstream_path(scope_path: str, raw_path: bytes | None) -> str | None:
    """The upstream path for a ``/mole/v1/`` request, or ``None`` when it escapes the prefix.

    The containment check runs on the fully decoded, normalized form; the path
    sent upstream is the request's raw path verbatim (so ``%2F`` inside a
    segment survives), falling back to the re-quoted decoded path when the raw
    path is missing or does not describe the same path.
    """
    raw = raw_path.decode("latin-1") if raw_path else None
    if raw is None or unquote(raw) != scope_path:
        raw = quote(scope_path, safe="/:@")
    for candidate in (scope_path, raw):
        decoded = _fully_decoded(candidate)
        if "\\" in decoded or "\x00" in decoded:
            return None
        normalized = posixpath.normpath(decoded)
        if not normalized.startswith(MOLE_PREFIX) or normalized == MOLE_PREFIX.rstrip("/"):
            return None
    return raw


def _query_without_api_key(query: bytes) -> bytes:
    """The raw query string with every ``api_key`` parameter (any case) removed."""
    kept = [
        part
        for part in query.split(b"&")
        if part and unquote_plus(part.split(b"=", 1)[0].decode("latin-1")).lower() != API_KEY_QUERY
    ]
    return b"&".join(kept)


def _mole_error(code: ErrorCode, detail: str, status_code: int) -> JSONResponse:
    return JSONResponse({"code": str(code), "detail": detail}, status_code=status_code)


def make_mole_forward(settings: Settings) -> Callable[[Request], Awaitable[Response]]:
    """The ``GET /mole/v1/{path}`` endpoint forwarding archiver reads to the backend."""
    base = httpx.URL(settings.base_url)
    base_path = base.raw_path.split(b"?", 1)[0].rstrip(b"/")
    headers = auth_headers(settings)

    async def forward(request: Request) -> Response:
        path = mole_upstream_path(request.scope["path"], request.scope.get("raw_path"))
        if path is None:
            return _mole_error(ErrorCode.NOT_FOUND, "path is not under /mole/v1/", 404)
        query = _query_without_api_key(request.scope.get("query_string", b""))
        raw_target = base_path + path.encode("ascii") + (b"?" + query if query else b"")
        url = base.copy_with(raw_path=raw_target)
        upstream_headers = {"Accept": request.headers.get("accept", "*/*"), **headers}
        client: httpx.AsyncClient = request.app.state.http
        try:
            upstream = await client.get(url, headers=upstream_headers, timeout=httpx.Timeout(MOLE_TIMEOUT))
        except httpx.TimeoutException:
            return _mole_error(
                ErrorCode.ARCHIVER_TIMEOUT, f"the archiver did not answer within {MOLE_TIMEOUT:g} s", 504
            )
        except httpx.TransportError as exc:
            return _mole_error(ErrorCode.ARCHIVER_UNAVAILABLE, f"the CANOPY API is unreachable: {exc}", 502)
        relayed = {}
        if "content-type" in upstream.headers:
            relayed["content-type"] = upstream.headers["content-type"]
        return Response(upstream.content, status_code=upstream.status_code, headers=relayed)

    return forward


async def not_allowed(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "canopy_mcp forwards the CANOPY API read-only: only the PV and formula WebSockets"
        " and GET /mole/v1/ are relayed",
        status_code=405,
        headers={"Allow": ""},
    )


def build_forward_routes(settings: Settings) -> list[BaseRoute]:
    """The two relays and the Mole GET, then a 405 for every other method and path of each service."""
    routes: list[BaseRoute] = [WebSocketRoute(path, make_relay(settings, path)) for path in RELAYED_PATHS]
    routes.append(Route(MOLE_PREFIX + "{path:path}", make_mole_forward(settings), methods=["GET"]))
    for service in SERVICE_PREFIXES:
        routes.append(Route(f"/{service}", not_allowed, methods=None))
        routes.append(Route(f"/{service}/{{path:path}}", not_allowed, methods=None))
    return routes
