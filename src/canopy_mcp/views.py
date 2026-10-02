"""Saved views: named Trace queries the viewer stores and reloads.

A view is ``{"id", "name", "query", "updated"}``: ``query`` is the Trace embed
query string (``pv=A:B&range=-1h``, plus Trace's axis and display settings as
``plot=<JSON>`` when the operator changed them), stored opaque and never
parsed here, ``name`` what the viewer shows, and
``updated`` the ISO-8601 UTC time of the last write. Each lives at
``<drafts_dir>/views/<id>.json``, a sibling of ``screens/``, so the drafts
manifest (which walks only ``screens/``) never lists one and the ``/drafts``
route (which serves only screens and images) never serves one.

As in :mod:`canopy_mcp.store`, this process is the only writer: one
``asyncio.Lock`` serialises writes, and every file is replaced atomically
(temp file in ``views/`` + ``os.replace``) so a reader never sees half a view.

HTTP surface (:func:`build_view_routes`), every response ``Cache-Control: no-cache``:

* ``GET /views`` -> 200 ``{"views": [...]}``, sorted by name (case-folded), then id
* ``GET /views/{id}`` -> 200 view, 404, 400 invalid id
* ``PUT /views/{id}`` with a JSON ``{"name", "query"}`` body -> 201 created,
  200 replaced (body: the stored view); 400 invalid id or body, 415 unless
  ``Content-Type: application/json``, 413 over :data:`MAX_BODY_BYTES`
* ``DELETE /views/{id}`` -> 204, 404, 400

Errors are ``{"error": "..."}``. The routes sit behind the OSPREY proxy like
the drafts routes and carry no auth of their own; as a CSRF guard, PUT and
DELETE answer 403 when ``Sec-Fetch-Site`` is present and is neither
``same-origin`` nor ``none``.
"""

import os
import re
import json
import asyncio
import tempfile
import unicodedata
from typing import Any
from pathlib import Path
from datetime import UTC, datetime

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

VIEW_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
VIEW_SUFFIX = ".json"
MAX_NAME_CHARS = 120
MAX_QUERY_BYTES = 4096
MAX_BODY_BYTES = 16 * 1024
VIEWS_CACHE_CONTROL = "no-cache"
# Every method is routed here (Starlette maps methods=None to GET only), so the
# 405s these handlers answer also carry Cache-Control.
ALL_METHODS = ["GET", "HEAD", "PUT", "DELETE", "POST", "PATCH", "OPTIONS"]
# Sec-Fetch-Site values a write may carry; any other value is a cross-site request.
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


class InvalidViewId(ValueError):
    """The id does not match ``^[A-Za-z0-9_-]{1,64}$``."""


class InvalidView(ValueError):
    """The name or query breaks the view rules."""


class ViewNotFound(LookupError):
    """No view is stored under the id."""


def validate_view_id(view_id: Any) -> str:
    if not isinstance(view_id, str) or not VIEW_ID_PATTERN.fullmatch(view_id):
        raise InvalidViewId(f"invalid view id: {view_id!r}")
    return view_id


def clean_name(name: Any) -> str:
    """``name`` stripped, when it is a 1..120-character string without control characters."""
    if not isinstance(name, str):
        raise InvalidView("name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise InvalidView(f"name must be 1 to {MAX_NAME_CHARS} characters after stripping")
    # Cs: a lone surrogate (JSON allows "\ud800") cannot be written as UTF-8.
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in name):
        raise InvalidView("name must not contain control characters or lone surrogates")
    return name


def check_query(query: Any) -> str:
    """``query`` when it is a string of at most 4096 UTF-8 bytes (empty allowed)."""
    if not isinstance(query, str):
        raise InvalidView("query must be a string")
    try:
        size = len(query.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidView("query must be valid UTF-8") from None
    if size > MAX_QUERY_BYTES:
        raise InvalidView(f"query must be at most {MAX_QUERY_BYTES} UTF-8 bytes")
    return query


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _serialize(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class ViewStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.views = self.root / "views"
        self._lock = asyncio.Lock()

    def path_for(self, view_id: str) -> Path:
        return self.views / f"{validate_view_id(view_id)}{VIEW_SUFFIX}"

    def _load(self, path: Path, view_id: str) -> dict[str, Any] | None:
        """The view stored at ``path`` when it is readable and valid, else ``None``."""
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("id") != view_id or not isinstance(doc.get("updated"), str):
            return None
        try:
            name, query = clean_name(doc.get("name")), check_query(doc.get("query"))
        except InvalidView:
            return None
        return {"id": view_id, "name": name, "query": query, "updated": doc["updated"]}

    def get(self, view_id: str) -> dict[str, Any]:
        view = self._load(self.path_for(view_id), view_id)
        if view is None:
            raise ViewNotFound(view_id)
        return view

    def list(self) -> list[dict[str, Any]]:
        """Every readable, valid view, sorted by case-folded name, then id."""
        if not self.views.is_dir():
            return []
        views = []
        for path in self.views.glob(f"*{VIEW_SUFFIX}"):
            view_id = path.name[: -len(VIEW_SUFFIX)]
            if not VIEW_ID_PATTERN.fullmatch(view_id) or not path.is_file():
                continue
            view = self._load(path, view_id)
            if view is not None:
                views.append(view)
        return sorted(views, key=lambda v: (v["name"].casefold(), v["id"]))

    async def put(self, view_id: str, name: Any, query: Any) -> tuple[dict[str, Any], bool]:
        """Create or replace the view; returns ``(view, created)``."""
        path = self.path_for(view_id)
        view = {"id": view_id, "name": clean_name(name), "query": check_query(query), "updated": _now()}
        data = _serialize(view)
        async with self._lock:
            created = not path.exists()
            self.views.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.views, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return view, created

    async def delete(self, view_id: str) -> None:
        path = self.path_for(view_id)
        async with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                raise ViewNotFound(view_id) from None


# --- HTTP ----------------------------------------------------------------------


def _json(body: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": VIEWS_CACHE_CONTROL, **(headers or {})}
    )


def _error(status_code: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return _json({"error": message}, status_code, headers)


def _cross_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    return site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES


def _is_json(request: Request) -> bool:
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media == "application/json"


async def _read_body(request: Request) -> bytes | None:
    """The request body, or ``None`` once it exceeds :data:`MAX_BODY_BYTES`."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None
        except ValueError:
            pass
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def build_view_routes(views: ViewStore) -> list[Route]:
    """``/views`` and ``/views/{id}`` over ``views``."""

    async def collection(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _error(405, "method not allowed", {"Allow": "GET, HEAD"})
        return _json({"views": views.list()})

    async def put(request: Request, view_id: str) -> Response:
        if not _is_json(request):
            return _error(415, "Content-Type must be application/json")
        raw = await _read_body(request)
        if raw is None:
            return _error(413, f"body exceeds {MAX_BODY_BYTES} bytes")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "body is not valid JSON")
        if not isinstance(body, dict) or set(body) != {"name", "query"}:
            return _error(400, 'body must be a JSON object with exactly "name" and "query"')
        try:
            view, created = await views.put(view_id, body["name"], body["query"])
        except InvalidView as exc:
            return _error(400, str(exc))
        return _json(view, 201 if created else 200)

    async def item(request: Request) -> Response:
        method = request.method
        if method not in ("GET", "HEAD", "PUT", "DELETE"):
            return _error(405, "method not allowed", {"Allow": "GET, HEAD, PUT, DELETE"})
        if method in ("PUT", "DELETE") and _cross_site(request):
            return _error(403, "cross-site writes are refused")
        try:
            view_id = validate_view_id(request.path_params["view_id"])
        except InvalidViewId as exc:
            return _error(400, str(exc))
        if method == "PUT":
            return await put(request, view_id)
        try:
            if method == "DELETE":
                await views.delete(view_id)
                return Response(status_code=204, headers={"Cache-Control": VIEWS_CACHE_CONTROL})
            return _json(views.get(view_id))
        except ViewNotFound:
            return _error(404, f"no view {view_id!r}")

    return [
        Route("/views", collection, methods=ALL_METHODS),
        # {path:path} so ids with a slash (always invalid) answer 400 here, not a bare 404.
        Route("/views/{view_id:path}", item, methods=ALL_METHODS),
    ]
