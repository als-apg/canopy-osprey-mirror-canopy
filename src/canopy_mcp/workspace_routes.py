"""The workspace hub's HTTP surface: what a CANOPY window says to the hub.

Served only when ``CANOPY_WORKSPACE`` is on (off by default); off, no route below
exists, so every path under ``/workspace`` is the router's 404 and ``config.json``
advertises nothing. A server that is shared between operators, or that runs more
than one worker process, must never turn it on: the hub is one in-memory object per
process serving one operator.

Paths are relative to :data:`WORKSPACE_PATH`, the path ``config.json`` advertises as
``{"workspace": {"path": "workspace", "protocol": 1}}``:

- ``POST claim[?if_vacant=1]`` ``{windowId, seq, shown, state, truncated?}``:
  :meth:`~canopy_mcp.workspace.Hub.claim`, ``200 {active, previous, epoch}``.
- ``PUT active`` ``{windowId, seq, shown, state, truncated?}``: the slot holder's
  report (:meth:`~canopy_mcp.workspace.Hub.report`), ``204``.
- ``DELETE active?window=<id>``: goodbye, no body, ``204`` whoever sends it.
- ``GET events?window=<id>``: the active window's event stream (server-sent events
  ``hello``, ``batch``, ``inactive`` and a ``: ping`` comment every
  :data:`PING_S` seconds); ``204`` for any other window, so its EventSource stops.
  One stream per window: a new one ends the old.
- ``POST active/{windowId}/result`` ``{batch, seq, state, results, truncated?}``:
  the one result of a batch the window holds
  (:meth:`~canopy_mcp.workspace.Hub.post_result`), ``204``.

There is no route that accepts commands: a batch reaches the hub only from the
``set_workspace`` MCP tool, in process.

Every route, the event stream included, first refuses a ``Sec-Fetch-Site`` that is
present and neither ``same-origin`` nor ``none`` with ``403 cross_site``, before it
reads the body or touches the hub (the rule the views and collections writes apply;
it is what protects the hub when no access token is configured). A body is JSON of
at most :data:`MAX_BODY_BYTES` (``413 too_large``, ``415`` for another media type,
``400 invalid_json``); a hub refusal is its :class:`~canopy_mcp.workspace.WorkspaceError`
code and status. Errors are ``{code, detail}`` with ``Cache-Control: no-cache``.

The hub is resolved per request (:func:`~canopy_mcp.workspace.get_hub`), so the
lifespan's reset swaps in a fresh one. Shutdown: open streams end on the server's exit
signal (sse-starlette watches it; uvicorn drains connections before the lifespan
shutdown runs), and :func:`exit_watch` closes the hub on the same signal so a pending
``set_workspace`` wait returns at once instead of holding the drain. The lifespan's
reset after that only wakes what is left.
"""

from __future__ import annotations

import json
import asyncio
import logging
from typing import Any
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sse_starlette.sse import AppStatus, EventSourceResponse
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response

from canopy_mcp.http import (
    NO_CACHE,
    ALL_METHODS,
    json_response,
    is_json_request,
    read_capped_body,
    refuse_cross_site,
    code_detail_response,
)
from canopy_mcp.workspace import Hub, Stream, StreamEnded, WorkspaceError, get_hub

# The path config.json advertises for these routes.
WORKSPACE_PATH = "workspace"

MAX_BODY_BYTES = 128 * 1024
PING_S = 15.0
WORKSPACE_CACHE_CONTROL = NO_CACHE
# How long one wait on a stream lasts before the loop looks again (the hub settles its timers then).
_NEXT_S = 1.0
# How often exit_watch looks for the server's exit signal.
_EXIT_POLL_S = 0.25

logger = logging.getLogger(__name__)


def _no_content() -> Response:
    return Response(status_code=204, headers={"Cache-Control": WORKSPACE_CACHE_CONTROL})


def _not_allowed(allow: str) -> Response:
    return code_detail_response(405, "method_not_allowed", f"allowed: {allow}", {"Allow": allow})


def _cross_site(request: Request) -> Response | None:
    return refuse_cross_site(
        request, lambda: code_detail_response(403, "cross_site", "cross-site requests are refused")
    )


def _refused(exc: WorkspaceError) -> Response:
    return json_response(exc.body(), exc.status)


async def _json_object(request: Request) -> tuple[dict[str, Any], Response | None]:
    """The JSON object body, or an error response (415, 413 over the cap, 400, 422 not an object)."""
    if not is_json_request(request):
        return {}, code_detail_response(415, "unsupported_media_type", "Content-Type must be application/json")
    raw = await read_capped_body(request, MAX_BODY_BYTES)
    if raw is None:
        return {}, code_detail_response(413, "too_large", f"the body exceeds {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw)
    except ValueError:
        return {}, code_detail_response(400, "invalid_json", "the body is not valid JSON")
    if not isinstance(body, dict):
        return {}, code_detail_response(422, "invalid_state", "the body must be a JSON object")
    return body, None


async def _events(stream: Stream) -> AsyncIterator[dict[str, str]]:
    """The stream's events as server-sent events; the window's stream is let go at the end."""
    try:
        while True:
            try:
                event = await stream.next(_NEXT_S)
            except StreamEnded:
                return
            if event is not None:
                yield {"event": event["event"], "data": json.dumps(event["data"], separators=(",", ":"))}
    finally:
        stream.hub.disconnect(stream)


def build_workspace_routes() -> list[Route]:
    """The workspace routes under ``/workspace``; mounted only with ``CANOPY_WORKSPACE`` on."""

    async def claim(request: Request) -> Response:
        if (refused := _cross_site(request)) is not None:
            return refused
        if request.method != "POST":
            return _not_allowed("POST")
        body, problem = await _json_object(request)
        if problem is not None:
            return problem
        try:
            answer = get_hub().claim(
                body.get("windowId"),
                body.get("seq"),
                body.get("shown"),
                body.get("state"),
                truncated=body.get("truncated", False),
                if_vacant=request.query_params.get("if_vacant") == "1",
            )
        except WorkspaceError as exc:
            return _refused(exc)
        return json_response(answer)

    async def active(request: Request) -> Response:
        if (refused := _cross_site(request)) is not None:
            return refused
        if request.method == "DELETE":
            window = request.query_params.get("window")
            if window is not None:
                get_hub().goodbye(window)
            return _no_content()
        if request.method != "PUT":
            return _not_allowed("PUT, DELETE")
        body, problem = await _json_object(request)
        if problem is not None:
            return problem
        try:
            get_hub().report(
                body.get("windowId"),
                body.get("seq"),
                body.get("shown"),
                body.get("state"),
                truncated=body.get("truncated", False),
            )
        except WorkspaceError as exc:
            return _refused(exc)
        return _no_content()

    async def events(request: Request) -> Response:
        if (refused := _cross_site(request)) is not None:
            return refused
        if request.method != "GET":
            return _not_allowed("GET")
        window = request.query_params.get("window")
        stream = get_hub().connect(window) if window is not None else None
        if stream is None:
            return _no_content()
        return EventSourceResponse(_events(stream), ping=PING_S, headers={"Cache-Control": WORKSPACE_CACHE_CONTROL})

    async def result(request: Request) -> Response:
        if (refused := _cross_site(request)) is not None:
            return refused
        if request.method != "POST":
            return _not_allowed("POST")
        body, problem = await _json_object(request)
        if problem is not None:
            return problem
        try:
            get_hub().post_result(
                request.path_params["window_id"],
                body.get("batch"),
                body.get("seq"),
                body.get("state"),
                body.get("results"),
                truncated=body.get("truncated", False),
            )
        except WorkspaceError as exc:
            return _refused(exc)
        return _no_content()

    base = f"/{WORKSPACE_PATH}"
    return [
        Route(f"{base}/claim", claim, methods=ALL_METHODS),
        Route(f"{base}/active", active, methods=ALL_METHODS),
        Route(f"{base}/events", events, methods=ALL_METHODS),
        Route(f"{base}/active/{{window_id}}/result", result, methods=ALL_METHODS),
    ]


def _server_exiting() -> bool:
    """The server's exit signal."""
    # sse-starlette sets this flag from uvicorn's exit handler; it is the signal that ends the event streams.
    return AppStatus.should_exit


@asynccontextmanager
async def exit_watch(hub_of: Any = get_hub) -> AsyncIterator[None]:
    """While entered, close the hub as soon as the server's exit signal is seen.

    Uvicorn waits for open requests before the lifespan shutdown runs; a ``set_workspace``
    call waiting on its batch is one of them, so it is woken here on the signal (it
    returns ``pending``) rather than by the lifespan's reset after the drain.
    """

    async def watch() -> None:
        while not _server_exiting():
            await asyncio.sleep(_EXIT_POLL_S)
        hub: Hub = hub_of()
        logger.info("workspace: exit signal, closing the hub")
        hub.close()

    task = asyncio.create_task(watch())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
