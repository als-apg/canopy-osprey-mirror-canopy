"""The non-MCP HTTP surface: the viewer, its ``config.json``, the draft screens it renders, Trace and health.

:func:`build_routes` returns every route; each handler's docstring states its
contract. The viewer and the react-trace pages are relative-URL builds: each
entry page gets a ``<base href>`` naming the prefix a reverse proxy forwards in
``X-Forwarded-Prefix`` (:func:`safe_prefix`), and the host's stylesheets when
``CANOPY_HOST_STYLESHEETS`` is set (:func:`inject_host_links`).
:func:`viewer_config` is the document the viewer reads first, and
:class:`SummaryReader` relays DATA's health summary into it and into
``/health/summary`` in hare mode.
"""

import re
import html
import json
import asyncio
import hashlib
import logging
from typing import Any
from pathlib import Path

import httpx
from pydantic import ValidationError
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, FileResponse, JSONResponse, PlainTextResponse
from starlette.applications import Starlette

from canopy_mcp.http import NO_CACHE, etag, not_modified
from canopy_mcp.store import (
    LIBRARY,
    SCREEN_SUFFIX,
    DraftStore,
    InvalidDraftId,
    contained_file,
)
from canopy_mcp.config import Settings
from canopy_mcp.validate import TOOL_IDS
from canopy.contracts.health import (
    DATA_DEGRADED,
    DATA_UNAVAILABLE,
    HealthDTO,
    ChannelCountsDTO,
    HareHealthSummaryDTO,
    FrontHealthSummaryDTO,
)
from canopy.core.color_scheme import get_color_scheme

PREFIX_HEADER = "x-forwarded-prefix"
PREFIX_PATTERN = re.compile(r"(/[A-Za-z0-9._~-]+)*")
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
SCREENS_PATH = "drafts"
# Headers on every non-screen draft response (200 and 304).
IMAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"}

TRACE_PATH = "trace/"
CORRELATION_PAGE = "correlation.html"
CORRELATION_PATH = TRACE_PATH + CORRELATION_PAGE
WAVEFORM_PAGE = "waveform.html"
WAVEFORM_PATH = TRACE_PATH + WAVEFORM_PAGE
# Each tool of :data:`~canopy_mcp.validate.TOOL_IDS` (the config.json key naming its page):
# the page it is served from under ``trace_dist`` and the route config.json names for it.
TOOL_PAGES: dict[str, tuple[str, str]] = {
    "trace": ("index.html", TRACE_PATH),
    "correlation": (CORRELATION_PAGE, CORRELATION_PATH),
    "waveform": (WAVEFORM_PAGE, WAVEFORM_PATH),
}
# On the trace index: same-origin, unsandboxed iframe of the viewer.
TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)

# DATA's summary, read by config.json and /health/summary in hare mode, and the bound on that ask.
SUMMARY_PATH = "/hare/v1/health/summary"
SUMMARY_TIMEOUT_S = 1.0

logger = logging.getLogger(__name__)

# Host theme id -> the viewer's light/dark mode. The host's own theme ids are
# not listed here: host styling arrives through the stylesheet links below.
THEMES: dict[str, str] = {"dark": "dark", "light": "light"}

_BASE_TAG = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html\b", re.IGNORECASE)


def safe_prefix(raw: str | None) -> str:
    """The forwarded prefix when it is a plain path, else ``''``; always HTML-escaped.

    ``X-Forwarded-Prefix`` is attacker-influenced, so anything but a plain
    slash-separated path falls back to the server root.
    """
    if not raw or not PREFIX_PATTERN.fullmatch(raw):
        return ""
    return html.escape(raw, quote=True)


def inject_base(index_html: str, prefix: str) -> str:
    """``index_html`` with exactly one ``<base href="{prefix}/">``, first in ``<head>``."""
    tag = f'<base href="{prefix}/">'
    stripped = _BASE_TAG.sub("", index_html)
    head = _HEAD_OPEN.search(stripped)
    if head is None:
        return tag + stripped
    return stripped[: head.end()] + tag + stripped[head.end() :]


def inject_host_links(page_html: str, links: str) -> str:
    """``page_html`` with ``links`` last in ``<head>`` and ``data-canopy-host`` on ``<html>``.

    ``links`` are the host's stylesheets (``CANOPY_HOST_STYLESHEETS``), one
    ``<link rel="stylesheet" data-canopy-host-sheet href="{prefix}/{path}">``
    each: last in ``<head>``, after the bundle's own stylesheet, so the host
    wins the cascade, and prefix-absolute on the Trace pages too, whose
    ``<base href>`` is deeper. A request with no prefix, or a malformed one,
    gets none. No script is ever added, so :data:`TRACE_CSP` is untouched.

    Unchanged when ``links`` is empty or the page has no ``<html>`` tag or no
    ``</head>``: the attribute without its stylesheets would style nothing.
    """
    if not links:
        return page_html
    root = _HTML_OPEN.search(page_html)
    close = _HEAD_CLOSE.search(page_html, root.end()) if root else None
    if root is None or close is None:
        return page_html
    return (
        page_html[: root.end()]
        + " data-canopy-host"
        + page_html[root.end() : close.start()]
        + links
        + page_html[close.start() :]
    )


def viewer_config(settings: Settings) -> dict[str, Any]:
    """The viewer's ``config.json``. It never carries a key: the viewer needs none.

    * ``confirmWrites`` (``CANOPY_CONFIRM_WRITES``): whether the viewer asks
      before each write; ``home`` the configured home screen;
    * each tool of :func:`configured_tools` as ``{path, query}``, ``query``
      being its default query;
    * ``colorScheme``: the flat scheme plus ``meta`` this server sees
      (:func:`canopy.core.color_scheme.get_color_scheme`), omitted when the
      flattener is unavailable.

    In hare mode the route adds ``backend`` and ``binding``
    (:func:`backend_identity`), and it adds the fields of the APIs mounted onto
    the app (``app.state.viewer_config_fields``, empty when none are). The
    document holds no secret, so it is served with
    ``Access-Control-Allow-Origin: *`` for cross-origin readers.
    """
    config: dict[str, Any] = {
        "pvMode": settings.pv_mode,
        "screensPath": SCREENS_PATH,
        "themes": dict(THEMES),
        "confirmWrites": settings.confirm_writes,
    }
    if settings.home_screen is not None:
        config["home"] = {"screen": settings.home_screen}
    for tool in configured_tools(settings):
        config[tool] = {"path": TOOL_PAGES[tool][1], "query": _default_query(settings, tool)}
    flat = get_color_scheme().flat_json
    if flat is not None:
        config["colorScheme"] = flat
    return config


def configured_tools(settings: Settings) -> tuple[str, ...]:
    """The :data:`TOOL_IDS` whose page this host serves, in tuple order (the tools config.json names).

    :data:`TOOL_IDS` is static: a stored collection member or tab may name any
    tool, whether or not this host serves its page.
    """
    return tuple(tool for tool in TOOL_IDS if _trace_page(settings, TOOL_PAGES[tool][0]) is not None)


def _default_query(settings: Settings, tool: str) -> str:
    """The query ``tool``'s page opens with when its URL carries none."""
    queries = {
        "trace": settings.trace_default_query,
        "correlation": settings.correlation_default_query,
        "waveform": settings.waveform_default_query,
    }
    return queries[tool]


class SummaryReader:
    """DATA's summary for one app: one request in flight, shared by every caller who asks meanwhile.

    Nothing is kept once a reading lands, so every answer is a reading taken
    during the caller's own request. A caller that goes away does not cancel
    the reading for the others. :meth:`read` never raises. A reading is shared
    only with callers on the event loop that owns it.

    The reader logs the summary's availability once per change: WARNING when it
    becomes unavailable or the reason changes, INFO when it is available again.
    No shared client, a timeout, any transport error, a non-200 or a body that
    is not the summary are all "unavailable".
    """

    def __init__(self) -> None:
        self._flight: asyncio.Future[HareHealthSummaryDTO | None] | None = None
        # Why the last reading was unavailable; None while the summary is available.
        self._reason: str | None = None

    async def read(self, app: Starlette) -> HareHealthSummaryDTO | None:
        """DATA's health summary over ``app``'s shared client, or ``None`` on any failure."""
        flight = self._flight
        # A flight is shared only with callers on the loop that owns it: one left pending
        # on a loop that closed can never land, so it is dropped, not awaited or cancelled.
        if flight is None or flight.get_loop() is not asyncio.get_running_loop():
            flight = self._flight = asyncio.ensure_future(self._fetch(getattr(app.state, "http", None)))
            # Registered before any waiter, so the slot is clear when they resume.
            flight.add_done_callback(self._landed)
        return await asyncio.shield(flight)

    def _landed(self, flight: asyncio.Future) -> None:
        if self._flight is flight:
            self._flight = None

    async def _fetch(self, client: httpx.AsyncClient | None) -> HareHealthSummaryDTO | None:
        """One request for the summary, or ``None`` on any failure.

        The whole exchange is bounded by :data:`SUMMARY_TIMEOUT_S`. The body counts
        only when it validates strictly as :class:`HareHealthSummaryDTO`: a count
        that is not a JSON integer, a missing field or a wrong type is no summary.
        Unknown extra fields are ignored.
        """
        if client is None:
            return None
        try:
            response = await asyncio.wait_for(client.get(SUMMARY_PATH), SUMMARY_TIMEOUT_S)
            if response.status_code != 200:
                return self._unavailable(f"HTTP {response.status_code}")
            summary = HareHealthSummaryDTO.model_validate_json(response.content, strict=True)
        except ValidationError as exc:
            return self._unavailable(f"the body is not the summary ({exc.error_count()} invalid fields)")
        except Exception as exc:  # timeout, transport: every caller must still answer
            return self._unavailable(repr(exc))
        if self._reason is not None:
            logger.info("DATA summary available")
            self._reason = None
        return summary

    def _unavailable(self, reason: str) -> None:
        """Log one unavailable reading: WARNING when ``reason`` is new, DEBUG while it repeats."""
        level = logging.DEBUG if reason == self._reason else logging.WARNING
        logger.log(level, "DATA summary unavailable: %s", reason)
        self._reason = reason


def backend_identity(summary: HareHealthSummaryDTO | None) -> dict[str, Any]:
    """``config.json``'s ``backend`` and ``binding`` keys for ``summary`` (both null without one)."""
    if summary is None:
        return {"backend": None, "binding": None}
    return {
        "backend": {"name": summary.backend, "label": summary.label, "notice": summary.notice},
        "binding": summary.binding,
    }


def health_summary_body(settings: Settings, summary: HareHealthSummaryDTO | None) -> FrontHealthSummaryDTO:
    """The rollup: this server's fallbacks, then DATA's own verdict, channel counts and ``reachable``.

    ``summary`` counts only in hare mode, where ``None`` means DATA's summary could not be
    read. The verdict is DATA's ``status``, relayed: the counts and ``reachable`` are
    reported, never judged.
    """
    causes = list(get_color_scheme().degraded)
    channels = reachable = None
    if settings.pv_mode == "hare":
        if summary is None:
            causes.append(DATA_UNAVAILABLE)
        else:
            channels = ChannelCountsDTO(connected=summary.connected, total=summary.active_monitors)
            reachable = summary.reachable
            if summary.status == "degraded":
                causes.append(DATA_DEGRADED)
    return FrontHealthSummaryDTO(
        status="degraded" if causes else "ok", degraded=causes, channels=channels, reachable=reachable
    )


def _trace_page(settings: Settings, name: str) -> Path | None:
    """The installed react-trace page ``name``, or ``None``."""
    dist = settings.trace_dist
    page = dist / name if dist else None
    return page if page is not None and page.is_file() else None


def _revved(request: Request, rev: str, make: Any, extra: dict[str, str] | None = None) -> Response:
    """``make()`` with the ETag of ``rev`` and ``no-cache``, or a 304 when the client holds that rev."""
    tag = etag(rev)
    headers = {"ETag": tag, "Cache-Control": NO_CACHE, **(extra or {})}
    if not_modified(request, tag):
        return Response(status_code=304, headers=headers)
    response = make()
    response.headers.update(headers)
    return response


def _file_under(root: Path, relpath: str) -> Path | None:
    """``root/relpath`` when it is a regular file inside ``root``, else ``None``."""
    if not relpath or "\x00" in relpath or "\\" in relpath:
        return None
    return contained_file(root, relpath)


def _missing() -> Response:
    return PlainTextResponse("not found", status_code=404)


def _screen_rev(store: DraftStore, target: Path) -> str | None:
    """The store's (cached) rev for a draft file, or ``None`` when ``target`` is not a draft."""
    screens = store.screens.resolve()
    if not target.is_relative_to(screens):
        return None
    try:
        return store.rev(target.relative_to(screens).as_posix()[: -len(SCREEN_SUFFIX)])
    except InvalidDraftId:
        return None


def build_routes(settings: Settings, store: DraftStore, library: DraftStore | None = None) -> list[Route]:
    """The viewer, config, draft, Trace, channel-catalog and health routes for ``settings``, ``store`` and ``library``.

    Draft screens carry ``Cache-Control: no-cache`` and an ETag equal to their
    rev: the viewer polls the manifest every second while a draft is open, and a
    304 keeps that poll cheap. Hashed build assets are cached for a year.
    """
    dist = settings.viewer_dist
    host_paths = settings.host_stylesheet_paths

    def _host_links(prefix: str) -> str:
        """The host's stylesheet links under ``prefix``, one per line; ``''`` without a prefix."""
        if not prefix:
            return ""
        return "".join(
            f'<link rel="stylesheet" data-canopy-host-sheet href="{prefix}/{html.escape(path, quote=True)}">\n'
            for path in host_paths
        )

    reader = SummaryReader()
    # path -> (stat key, sha256) of the non-screen draft files served, so an unchanged file is not re-read.
    file_revs: dict[Path, tuple[tuple[int, int, int], str]] = {}

    def file_rev(target: Path) -> str:
        st = target.stat()
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = file_revs.get(target)
        if cached is not None and cached[0] == key:
            return cached[1]
        rev = hashlib.sha256(target.read_bytes()).hexdigest()
        file_revs[target] = (key, rev)
        return rev

    async def index(request: Request) -> Response:
        page = dist / "index.html" if dist else None
        if page is None or not page.is_file():
            return PlainTextResponse("viewer not installed (set CANOPY_VIEWER_DIST)", status_code=503)
        prefix = safe_prefix(request.headers.get(PREFIX_HEADER))
        body = inject_base(page.read_text(encoding="utf-8"), prefix)
        body = inject_host_links(body, _host_links(prefix))
        return Response(body, media_type="text/html", headers={"Cache-Control": NO_CACHE})

    async def asset(request: Request) -> Response:
        target = _file_under(dist / "assets", request.path_params["path"]) if dist else None
        if target is None:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, headers={"Cache-Control": ASSET_CACHE_CONTROL})

    async def config(request: Request) -> Response:
        body = viewer_config(settings)
        body.update(getattr(request.app.state, "viewer_config_fields", {}))
        if settings.pv_mode == "hare":
            body.update(backend_identity(await reader.read(request.app)))
        return JSONResponse(
            body,
            headers={"Cache-Control": NO_CACHE, "Access-Control-Allow-Origin": "*"},
        )

    async def manifest(request: Request) -> Response:
        """The drafts, then the library's screens as ``{id, category: "library", path: "library/screens/..."}``."""
        listing = store.manifest()
        if library is not None:
            listing = {"screens": [*listing["screens"], *library.manifest()["screens"]]}
        body = json.dumps(listing, ensure_ascii=False).encode("utf-8")
        rev = hashlib.sha256(body).hexdigest()
        return _revved(request, rev, lambda: Response(body, media_type="application/json"))

    async def draft_file(request: Request) -> Response:
        """A draft screen or image; ``library/<path>`` is answered by the library store alone.

        The library store serves nothing outside its ``screens/``, so a report or
        any other file beside it is a 404. Images (everything that is not a
        screen) also carry ``Content-Security-Policy: default-src 'none'; sandbox``
        and ``nosniff``: they are copied in from legacy trees, so an ``.svg``
        opened directly must not run script and no body may be sniffed into a
        more capable type.
        """
        relpath = request.path_params["path"]
        owner = store
        if library is not None:
            first, _, rest = relpath.partition("/")
            if first == LIBRARY:
                owner, relpath = library, rest
        target = owner.resolve_static(relpath)
        if target is None:
            return PlainTextResponse("not found", status_code=404)
        is_screen = target.name.endswith(SCREEN_SUFFIX)
        rev = _screen_rev(owner, target) if is_screen else None
        if rev is None:
            rev = file_rev(target)
        media = "application/json" if is_screen else None
        extra = None if is_screen else IMAGE_HEADERS
        return _revved(request, rev, lambda: FileResponse(target, media_type=media), extra)

    def trace_html(request: Request, page: Path | None) -> Response:
        """A react-trace entry page, based at ``{prefix}/trace/``."""
        if page is None:
            return _missing()
        prefix = safe_prefix(request.headers.get(PREFIX_HEADER))
        body = inject_base(page.read_text(encoding="utf-8"), prefix + "/trace")
        body = inject_host_links(body, _host_links(prefix))
        headers = {"Cache-Control": NO_CACHE, "Content-Security-Policy": TRACE_CSP}
        return Response(body, media_type="text/html", headers=headers)

    async def trace_index(request: Request) -> Response:
        """Trace at exactly ``/trace/``; the bare ``/trace`` and every other path under it are 404s, never redirects."""
        return trace_html(request, _trace_page(settings, "index.html"))

    async def correlation_page(request: Request) -> Response:
        """react-trace's correlation page, served like the index; a 404 (and not in config.json) when not installed."""
        return trace_html(request, _trace_page(settings, CORRELATION_PAGE))

    async def waveform_page(request: Request) -> Response:
        return trace_html(request, _trace_page(settings, WAVEFORM_PAGE))

    async def trace_asset(request: Request) -> Response:
        trace = settings.trace_dist
        target = _file_under(trace / "assets", request.path_params["path"]) if trace else None
        if target is None:
            return _missing()
        return FileResponse(target, headers={"Cache-Control": ASSET_CACHE_CONTROL})

    async def trace_favicon(request: Request) -> Response:
        trace = settings.trace_dist
        target = _file_under(trace, "favicon.svg") if trace else None
        if target is None:
            return _missing()
        return FileResponse(target, headers={"Cache-Control": NO_CACHE})

    async def channels(request: Request) -> Response:
        """The channel catalog ``CANOPY_CHANNELS_FILE`` names (a JSON array of addresses), for PV autocomplete.

        The deployment's build writes the file. It is re-read on every request
        and served with a strong ETag over its bytes; unset or absent is a 404:
        no catalog, not an empty one. It sits at the server root beside
        ``/mole/v1/``, so the Trace pages reach it as ``{apiBase}/channels``.
        """
        path = settings.channels_file
        try:
            payload = path.read_bytes() if path else None
        except OSError:
            payload = None
        if payload is None:
            return _missing()
        rev = hashlib.sha256(payload).hexdigest()
        return _revved(request, rev, lambda: Response(payload, media_type="application/json"))

    async def not_found(request: Request) -> Response:
        return _missing()

    async def health(request: Request) -> Response:
        """canopy's own :class:`HealthDTO`: reads nothing from DATA or the scheme, so it never varies by mode."""
        return JSONResponse(HealthDTO.of().model_dump(by_alias=True, mode="json"))

    async def health_summary(request: Request) -> Response:
        """The rollup (:func:`health_summary_body`); 200 on every branch."""
        summary = await reader.read(request.app) if settings.pv_mode == "hare" else None
        body = health_summary_body(settings, summary).model_dump(by_alias=True, mode="json")
        return JSONResponse(body, headers={"Cache-Control": NO_CACHE})

    return [
        Route("/", index, methods=["GET", "HEAD"]),
        Route("/s/{path:path}", index, methods=["GET", "HEAD"]),
        Route("/embed/{path:path}", index, methods=["GET", "HEAD"]),
        Route("/assets/{path:path}", asset, methods=["GET", "HEAD"]),
        Route("/config.json", config, methods=["GET"]),
        Route("/drafts/manifest.json", manifest, methods=["GET", "HEAD"]),
        Route("/drafts/{path:path}", draft_file, methods=["GET", "HEAD"]),
        Route("/health", health, methods=["GET"]),
        Route("/health/summary", health_summary, methods=["GET"]),
        Route("/channels", channels, methods=["GET", "HEAD"]),
        Route("/trace/", trace_index, methods=["GET", "HEAD"]),
        Route("/trace/assets/{path:path}", trace_asset, methods=["GET", "HEAD"]),
        Route("/trace/favicon.svg", trace_favicon, methods=["GET", "HEAD"]),
        Route(f"/{CORRELATION_PATH}", correlation_page, methods=["GET", "HEAD"]),
        Route(f"/{WAVEFORM_PATH}", waveform_page, methods=["GET", "HEAD"]),
        # Claim the rest of /trace so the router never slash-redirects into it.
        Route("/trace", not_found),
        Route("/trace/{path:path}", not_found),
    ]
