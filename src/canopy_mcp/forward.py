"""The read-only forward of the CANOPY API: two WebSocket relays and nothing else.

The served viewer reads live values over ``WS /hare/v1/ws/pvs`` and
``WS /fox/v1/ws/formulas``. Both are relayed to ``CANOPY_BASE_URL`` with the
server's own ``X-API-Key`` (``CANOPY_API_KEY``) on the upstream handshake. The
upstream handshake is built from scratch -- no client header is copied and any
``api_key`` query parameter is dropped -- so a browser can neither see the key
nor substitute its own.

Every other method and path under the backend's service prefixes answers 405,
in particular ``PUT /hare/v1/pvs/*`` (a PV write) and
``POST /squirrel/*/restore``: this process fronts the backend read-only.
WebSocket connections to any other path fall through to Starlette's default,
which refuses the handshake.
"""

from urllib.parse import urlsplit, urlencode, urlunsplit
from collections.abc import Callable, Iterable, Awaitable

import anyio
from websockets import ConnectionClosed, InvalidHandshake
from starlette.routing import Route, BaseRoute, WebSocketRoute
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocket, WebSocketState
from websockets.asyncio.client import ClientConnection, connect

from canopy_mcp.client import auth_headers
from canopy_mcp.config import Settings

RELAYED_PATHS = ("/hare/v1/ws/pvs", "/fox/v1/ws/formulas")
SERVICE_PREFIXES = ("hare", "fox", "squirrel", "beaver", "raccoon")
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
            upstream = await connect(
                url, additional_headers=headers, open_timeout=OPEN_TIMEOUT, max_size=None, user_agent_header=None
            )
        except (OSError, TimeoutError, InvalidHandshake):
            # Closing before accept refuses the client's handshake.
            await websocket.close(code=1011)
            return
        async with upstream:
            await websocket.accept()
            await _pipe(websocket, upstream)

    return relay


async def not_allowed(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "canopy_mcp forwards the CANOPY API read-only: only the PV and formula WebSockets are relayed",
        status_code=405,
        headers={"Allow": ""},
    )


def build_forward_routes(settings: Settings) -> list[BaseRoute]:
    """The two relays, then a 405 for every other method and path of each service."""
    routes: list[BaseRoute] = [WebSocketRoute(path, make_relay(settings, path)) for path in RELAYED_PATHS]
    for service in SERVICE_PREFIXES:
        routes.append(Route(f"/{service}", not_allowed, methods=None))
        routes.append(Route(f"/{service}/{{path:path}}", not_allowed, methods=None))
    return routes
