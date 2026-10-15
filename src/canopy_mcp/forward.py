"""The forward of the CANOPY API: two WebSocket relays, the Mole reads, two Fox POSTs and the opt-in PV write.

The served viewer reads live values over ``WS /hare/v1/ws/pvs`` and
``WS /fox/v1/ws/formulas``. Both are relayed to ``CANOPY_BASE_URL`` with the
server's own ``X-API-Key`` (``CANOPY_API_KEY``) on the upstream handshake. The
upstream handshake is built from scratch -- no client header is copied and any
``api_key`` query parameter is dropped -- so a browser can neither see the key
nor substitute its own. An upstream that is not accepting connections yet is
retried for ``CANOPY_RELAY_CONNECT_RETRY_S`` seconds (default 0: one attempt)
before the browser's handshake is refused with close code 1011.

``GET /mole/v1/{path}`` (the Trace tab's archiver reads) is forwarded the same
way over HTTP: only ``Accept`` and the server key go upstream, ``api_key`` is
dropped, and a path that would leave ``/mole/v1/`` once decoded is a 404.

``POST /fox/v1/validate`` and ``POST /fox/v1/evaluate`` (read access on Fox,
used by Trace's formula curves) are forwarded under the same rules: only
``Content-Type``, ``Accept`` and the server key go upstream, ``api_key`` is
dropped, and the upstream path is the route's own fixed path, so nothing a
client encodes into it can leave ``/fox/v1/``. A request whose
``Sec-Fetch-Site`` is present and not in
:data:`canopy_mcp.views.ALLOWED_FETCH_SITES` is refused 403, a body over
:data:`FOX_BODY_MAX_BYTES` 413, an upstream that does not answer within
:data:`FOX_TIMEOUT` seconds is 504 ``formula_eval_timeout`` and one that cannot
be reached 502, all as ``{code, detail}``. Upstream's status, body and content
type are returned verbatim.

With ``CANOPY_PUT_RELAY`` on, ``PUT /hare/v1/pvs/{pv}`` -- and nothing else --
is relayed exactly: the raw path (no query string) after the same containment
check under ``/hare/v1/pvs/`` with a non-empty PV, only ``Content-Type`` and the
write key (``CANOPY_WRITE_API_KEY_FILE``) as headers, a body of at most
:data:`PUT_BODY_MAX_BYTES`, and upstream's status, body and content type
returned verbatim. A request whose ``Sec-Fetch-Site`` is present and not in
:data:`canopy_mcp.views.ALLOWED_FETCH_SITES` is refused 403, an escaping or
empty PV path 404 and an oversized body 413, all with WC6 row 14b's envelope;
an upstream that does not answer within ``CANOPY_PUT_RELAY_TIMEOUT_S`` is 504
and one that cannot be reached 502, both ``pv_write_unknown`` with row 13's
text, since the write may or may not have happened.

Every other method and path under the backend's service prefixes answers 405,
in particular ``PUT /hare/v1/pvs/*`` with the relay off,
``POST /squirrel/*/restore`` and every Fox path but the two POSTs above. WebSocket connections to any other path fall
through to Starlette's default, which refuses the handshake.
"""

import time
import posixpath
from typing import Any
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

from canopy.contracts import load_wc6_vectors
from canopy_mcp.views import ALLOWED_FETCH_SITES
from canopy_mcp.client import API_KEY_HEADER, auth_headers
from canopy_mcp.config import Settings
from canopy.core.errors import ErrorCode

RELAYED_PATHS = ("/hare/v1/ws/pvs", "/fox/v1/ws/formulas")
SERVICE_PREFIXES = ("hare", "fox", "squirrel", "raccoon", "mole")
MOLE_PREFIX = "/mole/v1/"
MOLE_TIMEOUT = 15.0
# The Fox HTTP endpoints relayed (POST only), both read access upstream.
FOX_POST_PATHS = ("/fox/v1/validate", "/fox/v1/evaluate")
# Largest formula request body the relay accepts (a JSON spec, plus a window for evaluate).
FOX_BODY_MAX_BYTES = 256 * 1024
# Evaluate may read an archive window before computing, so it gets longer than a Mole read.
FOX_TIMEOUT = 30.0
FOX_UNAVAILABLE = "formula_service_unavailable"
PVS_PREFIX = "/hare/v1/pvs/"
# Largest PV write body the relay accepts (a JSON ``{"value": ...}``).
PUT_BODY_MAX_BYTES = 64 * 1024
API_KEY_QUERY = "api_key"
OPEN_TIMEOUT = 10.0
# Pause between connect attempts inside the retry window.
CONNECT_RETRY_INTERVAL = 0.1

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


async def _connect_upstream(url: str, headers: dict[str, str], retry_s: float) -> ClientConnection:
    """Open the upstream connection, retrying a failed connect for ``retry_s`` seconds.

    Only a connection that could not be made (refused, unreachable) is
    retried: that is a backend still coming up. A connect that timed out or an
    upstream that answered and rejected the handshake is final. The last
    failure propagates once the window is over.
    """
    deadline = time.monotonic() + retry_s
    while True:
        try:
            # compression=None: the in-cluster hop never negotiates permessage-deflate.
            return await connect(
                url,
                additional_headers=headers,
                open_timeout=OPEN_TIMEOUT,
                max_size=None,
                user_agent_header=None,
                compression=None,
            )
        except TimeoutError:
            raise
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            await anyio.sleep(min(CONNECT_RETRY_INTERVAL, remaining))


def make_relay(settings: Settings, path: str) -> Callable[[WebSocket], Awaitable[None]]:
    """The WebSocket endpoint relaying ``path`` to the backend."""
    headers = auth_headers(settings)

    async def relay(websocket: WebSocket) -> None:
        url = upstream_ws_url(settings.base_url, path, websocket.query_params.multi_items())
        try:
            upstream = await _connect_upstream(url, headers, settings.relay_connect_retry_s)
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


def mole_upstream_path(scope_path: str, raw_path: bytes | None, prefix: str = MOLE_PREFIX) -> str | None:
    """The upstream path for a request under ``prefix``, or ``None`` when it escapes it.

    ``prefix`` ends in ``/`` (``/mole/v1/`` by default, ``/hare/v1/pvs/`` for the
    PUT relay); a path naming nothing below it is ``None`` too. The containment
    check runs on the fully decoded, normalized form; the path sent upstream is
    the request's raw path verbatim (so ``%2F`` inside a segment and the ``//``
    of a ``ca://`` PV survive), falling back to the re-quoted decoded path when
    the raw path is missing or does not describe the same path.
    """
    raw = raw_path.decode("latin-1") if raw_path else None
    if raw is None or unquote(raw) != scope_path:
        raw = quote(scope_path, safe="/:@")
    for candidate in (scope_path, raw):
        decoded = _fully_decoded(candidate)
        if "\\" in decoded or "\x00" in decoded:
            return None
        normalized = posixpath.normpath(decoded)
        if not normalized.startswith(prefix) or normalized == prefix.rstrip("/"):
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


def _mole_error(code: ErrorCode | str, detail: str, status_code: int) -> JSONResponse:
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


def make_fox_post(settings: Settings, path: str) -> Callable[[Request], Awaitable[Response]]:
    """The ``POST`` endpoint for one of :data:`FOX_POST_PATHS`, forwarding it to the backend."""
    base = httpx.URL(settings.base_url)
    base_path = base.raw_path.split(b"?", 1)[0].rstrip(b"/")
    headers = auth_headers(settings)
    timeout = httpx.Timeout(FOX_TIMEOUT)

    async def forward(request: Request) -> Response:
        site = request.headers.get("sec-fetch-site")
        if site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES:
            return _mole_error(ErrorCode.BAD_REQUEST, "cross-site requests are not relayed", 403)
        # The route matched ``path`` exactly; the fixed path is what goes upstream.
        if request.scope["path"] != path:
            return _mole_error(ErrorCode.NOT_FOUND, f"path is not {path}", 404)
        body = await _capped_body(request, FOX_BODY_MAX_BYTES)
        if body is None:
            return _mole_error(ErrorCode.BAD_REQUEST, f"the request body exceeds {FOX_BODY_MAX_BYTES} bytes", 413)
        query = _query_without_api_key(request.scope.get("query_string", b""))
        raw_target = base_path + path.encode("ascii") + (b"?" + query if query else b"")
        url = base.copy_with(raw_path=raw_target)
        # Built from scratch: no client header but Content-Type and Accept, and the server key.
        upstream_headers = {"Accept": request.headers.get("accept", "*/*")}
        if "content-type" in request.headers:
            upstream_headers["Content-Type"] = request.headers["content-type"]
        upstream_headers.update(headers)
        client: httpx.AsyncClient = request.app.state.http
        try:
            upstream = await client.post(url, content=body, headers=upstream_headers, timeout=timeout)
        except httpx.TimeoutException:
            return _mole_error(
                ErrorCode.FORMULA_EVAL_TIMEOUT, f"the formula service did not answer within {FOX_TIMEOUT:g} s", 504
            )
        except httpx.TransportError as exc:
            return _mole_error(FOX_UNAVAILABLE, f"the CANOPY API is unreachable: {exc}", 502)
        relayed = {}
        if "content-type" in upstream.headers:
            relayed["content-type"] = upstream.headers["content-type"]
        return Response(upstream.content, status_code=upstream.status_code, headers=relayed)

    return forward


def _row_reply(rows: dict[str, Any], row: str, status_code: int | None = None) -> Callable[[], JSONResponse]:
    """A factory of WC6 row ``row``'s ``{code, detail}`` reply, at the row's status unless one is given."""
    vector = rows[row]
    body = {"code": vector["code"], "detail": vector["detail"]}
    status = vector["status"] if status_code is None else status_code
    return lambda: JSONResponse(body, status_code=status)


async def _capped_body(request: Request, limit: int) -> bytes | None:
    """The request body read as a stream, or ``None`` as soon as it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def make_put_relay(settings: Settings) -> Callable[[Request], Awaitable[Response]]:
    """The ``PUT /hare/v1/pvs/{pv}`` endpoint relaying one PV write to the backend."""
    base = httpx.URL(settings.base_url)
    base_path = base.raw_path.split(b"?", 1)[0].rstrip(b"/")
    write_key = settings.write_api_key
    timeout = httpx.Timeout(settings.put_relay_timeout_s)
    rows = load_wc6_vectors()["rows"]
    # FRONT's own refusals (row 14b): nothing was sent upstream.
    cross_site, escaping, oversized = (_row_reply(rows, f"14b-{status}") for status in (403, 404, 413))
    # The write went out and no answer came back: row 13's code and text.
    timed_out, unreachable = _row_reply(rows, "13", 504), _row_reply(rows, "13", 502)

    async def relay(request: Request) -> Response:
        site = request.headers.get("sec-fetch-site")
        if site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES:
            return cross_site()
        path = mole_upstream_path(request.scope["path"], request.scope.get("raw_path"), PVS_PREFIX)
        if path is None:
            return escaping()
        body = await _capped_body(request, PUT_BODY_MAX_BYTES)
        if body is None:
            return oversized()
        # Built from scratch: no client header but Content-Type, never the read key.
        upstream_headers = {API_KEY_HEADER: write_key}
        if "content-type" in request.headers:
            upstream_headers["Content-Type"] = request.headers["content-type"]
        url = base.copy_with(raw_path=base_path + path.encode("ascii"))
        client: httpx.AsyncClient = request.app.state.http
        try:
            upstream = await client.put(url, content=body, headers=upstream_headers, timeout=timeout)
        except httpx.TimeoutException:
            return timed_out()
        except httpx.TransportError:
            return unreachable()
        relayed = {}
        if "content-type" in upstream.headers:
            relayed["content-type"] = upstream.headers["content-type"]
        return Response(upstream.content, status_code=upstream.status_code, headers=relayed)

    return relay


async def not_allowed(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "canopy_mcp forwards the CANOPY API read-only: only the PV and formula WebSockets"
        ", GET /mole/v1/ and POST /fox/v1/validate and /fox/v1/evaluate are relayed",
        status_code=405,
        headers={"Allow": ""},
    )


def build_forward_routes(settings: Settings) -> list[BaseRoute]:
    """The two relays, the Mole GET, the Fox POSTs and (relay on) the PV PUT, then a 405 for everything else."""
    routes: list[BaseRoute] = [WebSocketRoute(path, make_relay(settings, path)) for path in RELAYED_PATHS]
    routes.append(Route(MOLE_PREFIX + "{path:path}", make_mole_forward(settings), methods=["GET"]))
    routes.extend(Route(path, make_fox_post(settings, path), methods=["POST"]) for path in FOX_POST_PATHS)
    if settings.put_relay:
        routes.append(Route(PVS_PREFIX + "{pv:path}", make_put_relay(settings), methods=["PUT"]))
    for service in SERVICE_PREFIXES:
        routes.append(Route(f"/{service}", not_allowed, methods=None))
        routes.append(Route(f"/{service}/{{path:path}}", not_allowed, methods=None))
    return routes
