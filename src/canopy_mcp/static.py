"""The non-MCP HTTP surface: the viewer, its config, and the draft screens it renders.

The viewer is a relative-URL build (vite ``base: './'``), so it works at any
path as long as ``index.html`` carries a ``<base href>`` naming the prefix it
is served under. Behind the OSPREY proxy that prefix arrives as
``X-Forwarded-Prefix``; it is attacker-influenced, so it is accepted only when
it is a plain slash-separated path and is HTML-escaped on the way in, and
anything else falls back to the server root.

Draft screens are served with ``Cache-Control: no-cache`` and an ETag equal to
their rev: the viewer polls the manifest every second while a draft is open,
and a 304 keeps that poll cheap. Hashed build assets never change under the
same name, so they are cached for a year.

Draft images (anything under the drafts route that is not a screen) also carry
``Content-Security-Policy: default-src 'none'; sandbox`` and
``X-Content-Type-Options: nosniff``: they are copied in from legacy trees, so an
``.svg`` opened directly must not run script and no body may be sniffed into a
more capable type.

The built react-trace is served at exactly ``/trace/`` (its index, with a
``<base href>`` of ``{prefix}/trace/`` and :data:`TRACE_CSP`), ``/trace/assets/``
and ``/trace/favicon.svg``. Every other ``/trace`` path, the bare ``/trace``
included, is a 404 rather than a slash redirect. ``config.json`` names the
route only when ``trace_dist/index.html`` exists.

react-trace's correlation page, ``trace_dist/correlation.html``, is served at
``/trace/correlation.html`` exactly like the Trace index (same ``<base href>``
of ``{prefix}/trace/``, :data:`TRACE_CSP` and no-cache), and ``config.json``
names it as ``{"correlation": {"path": "trace/correlation.html", "query": ...}}``
only when the file exists, so a dist built from an upstream react-trace without
the page advertises nothing and 404s there.

``GET /channels`` serves the build-time channel catalog named by
``CANOPY_CHANNELS_FILE`` (a JSON array of channel addresses, written by
``osprey build`` and mounted read-only) for the Trace pages' PV
autocomplete. It sits at the server root beside ``/mole/v1/``, so the pages
reach it as ``{apiBase}/channels``. Like OSPREY's bluesky_web route it carries a
strong ETag over the file bytes and ``Cache-Control: no-cache``, re-reads the
file on every request, and answers 404 when the variable is unset or the file
is absent: no catalog, not an empty one.

``config.json`` always names the saved-views API (``{"views": {"path": "views"}}``):
``/views`` and ``/views/{id}`` are served by :mod:`canopy_mcp.views` from
``<drafts_dir>/views/``, beside (never inside) ``screens/``, so a view is
neither listed in the drafts manifest nor served under ``/drafts``.
"""

import re
import html
import json
import hashlib
from typing import Any
from pathlib import Path

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, FileResponse, JSONResponse, PlainTextResponse

from canopy_mcp.store import SCREEN_SUFFIX, DraftStore, InvalidDraftId, contained_file
from canopy_mcp.config import Settings

PREFIX_HEADER = "x-forwarded-prefix"
PREFIX_PATTERN = re.compile(r"(/[A-Za-z0-9._~-]+)*")
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
DRAFT_CACHE_CONTROL = "no-cache"
SCREENS_PATH = "drafts"
VIEWS_PATH = "views"
# Headers on every non-screen draft response (200 and 304).
IMAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"}

TRACE_PATH = "trace/"
CORRELATION_PAGE = "correlation.html"
CORRELATION_PATH = TRACE_PATH + CORRELATION_PAGE
# On the trace index: same-origin, unsandboxed iframe of the viewer.
TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)

# OSPREY theme id -> the viewer's light/dark mode.
THEMES: dict[str, str] = {"dark": "dark", "light": "light"}

_BASE_TAG = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


def safe_prefix(raw: str | None) -> str:
    """The forwarded prefix when it is a plain path, else ``''``; always HTML-escaped."""
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


def viewer_config(settings: Settings) -> dict[str, Any]:
    """The viewer's ``config.json``. It never carries a key: the viewer needs none."""
    config: dict[str, Any] = {
        "pvMode": settings.pv_mode,
        "screensPath": SCREENS_PATH,
        "themes": dict(THEMES),
        "views": {"path": VIEWS_PATH},
    }
    if _trace_index(settings) is not None:
        config["trace"] = {"path": TRACE_PATH, "query": settings.trace_default_query}
    if _trace_page(settings, CORRELATION_PAGE) is not None:
        config["correlation"] = {"path": CORRELATION_PATH, "query": settings.correlation_default_query}
    return config


def _trace_page(settings: Settings, name: str) -> Path | None:
    """The installed react-trace page ``name``, or ``None``."""
    dist = settings.trace_dist
    page = dist / name if dist else None
    return page if page is not None and page.is_file() else None


def _trace_index(settings: Settings) -> Path | None:
    """The installed react-trace ``index.html``, or ``None``."""
    return _trace_page(settings, "index.html")


def _etag(rev: str) -> str:
    return f'"{rev}"'


def _not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return etag in candidates or "*" in candidates


def _revved(request: Request, rev: str, make: Any, extra: dict[str, str] | None = None) -> Response:
    etag = _etag(rev)
    headers = {"ETag": etag, "Cache-Control": DRAFT_CACHE_CONTROL, **(extra or {})}
    if _not_modified(request, etag):
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


def build_routes(settings: Settings, store: DraftStore) -> list[Route]:
    """The viewer, config, draft and health routes for ``settings`` and ``store``."""
    dist = settings.viewer_dist

    async def index(request: Request) -> Response:
        page = dist / "index.html" if dist else None
        if page is None or not page.is_file():
            return PlainTextResponse("viewer not installed (set CANOPY_VIEWER_DIST)", status_code=503)
        prefix = safe_prefix(request.headers.get(PREFIX_HEADER))
        body = inject_base(page.read_text(encoding="utf-8"), prefix)
        return Response(body, media_type="text/html", headers={"Cache-Control": DRAFT_CACHE_CONTROL})

    async def asset(request: Request) -> Response:
        target = _file_under(dist / "assets", request.path_params["path"]) if dist else None
        if target is None:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, headers={"Cache-Control": ASSET_CACHE_CONTROL})

    async def config(request: Request) -> Response:
        return JSONResponse(viewer_config(settings), headers={"Cache-Control": DRAFT_CACHE_CONTROL})

    async def manifest(request: Request) -> Response:
        body = json.dumps(store.manifest(), ensure_ascii=False).encode("utf-8")
        rev = hashlib.sha256(body).hexdigest()
        return _revved(request, rev, lambda: Response(body, media_type="application/json"))

    async def draft_file(request: Request) -> Response:
        relpath = request.path_params["path"]
        target = store.resolve_static(relpath)
        if target is None:
            return PlainTextResponse("not found", status_code=404)
        is_screen = target.name.endswith(SCREEN_SUFFIX)
        rev = _screen_rev(store, target) if is_screen else None
        if rev is None:
            rev = hashlib.sha256(target.read_bytes()).hexdigest()
        media = "application/json" if is_screen else None
        extra = None if is_screen else IMAGE_HEADERS
        return _revved(request, rev, lambda: FileResponse(target, media_type=media), extra)

    def trace_html(request: Request, page: Path | None) -> Response:
        """A react-trace entry page, based at ``{prefix}/trace/``."""
        if page is None:
            return _missing()
        prefix = safe_prefix(request.headers.get(PREFIX_HEADER))
        body = inject_base(page.read_text(encoding="utf-8"), prefix + "/trace")
        headers = {"Cache-Control": DRAFT_CACHE_CONTROL, "Content-Security-Policy": TRACE_CSP}
        return Response(body, media_type="text/html", headers=headers)

    async def trace_index(request: Request) -> Response:
        return trace_html(request, _trace_index(settings))

    async def correlation_page(request: Request) -> Response:
        return trace_html(request, _trace_page(settings, CORRELATION_PAGE))

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
        return FileResponse(target, headers={"Cache-Control": DRAFT_CACHE_CONTROL})

    async def channels(request: Request) -> Response:
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
        return JSONResponse({"status": "ok"})

    return [
        Route("/", index, methods=["GET", "HEAD"]),
        Route("/s/{path:path}", index, methods=["GET", "HEAD"]),
        Route("/embed/{path:path}", index, methods=["GET", "HEAD"]),
        Route("/assets/{path:path}", asset, methods=["GET", "HEAD"]),
        Route("/config.json", config, methods=["GET"]),
        Route("/drafts/manifest.json", manifest, methods=["GET", "HEAD"]),
        Route("/drafts/{path:path}", draft_file, methods=["GET", "HEAD"]),
        Route("/health", health, methods=["GET"]),
        Route("/channels", channels, methods=["GET", "HEAD"]),
        Route("/trace/", trace_index, methods=["GET", "HEAD"]),
        Route("/trace/assets/{path:path}", trace_asset, methods=["GET", "HEAD"]),
        Route("/trace/favicon.svg", trace_favicon, methods=["GET", "HEAD"]),
        Route(f"/{CORRELATION_PATH}", correlation_page, methods=["GET", "HEAD"]),
        # Claim the rest of /trace so the router never slash-redirects into it.
        Route("/trace", not_found),
        Route("/trace/{path:path}", not_found),
    ]
