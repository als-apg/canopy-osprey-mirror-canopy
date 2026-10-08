"""Saved views: named Trace queries the viewer stores and reloads.

A view is ``{"id", "name", "query", "kind", "updated"}``: ``kind`` names the
tool that opens it (default ``trace``; rows stored without one read as
``trace``; any ``^[a-z][a-z0-9-]{0,31}$`` kind is stored and listed), ``query`` is the Trace embed
query string (``pv=A:B&range=-1h``, plus Trace's axis and display settings as
``plot=<JSON>`` when the operator changed them), stored opaque and never
parsed here, ``name`` what the viewer shows, and
``updated`` the ISO-8601 UTC time of the last write. Each lives at
``<views dir>/<id>.json``. The views directory is ``CANOPY_VIEWS_DIR`` when
that is set and ``<drafts_dir>/views`` otherwise, a sibling of ``screens/``,
so the drafts manifest (which walks only ``screens/``) never lists one and
the ``/drafts`` route (which serves only screens and images) never serves one.

As in :mod:`canopy_mcp.store`, other processes may write the same directory:
every write is one synchronous section in a worker thread under an exclusive
``flock`` on the views directory itself (never on the drafts root, and with no
lock file), behind the store's ``asyncio.Lock``. Every file is replaced atomically
(temp file in the views directory + ``os.replace``) so a reader never sees half a view.

HTTP surface (:func:`build_view_routes`), every response ``Cache-Control: no-cache``:

* ``GET /views`` -> 200 ``{"views": [...]}``, sorted by name (case-folded), then id
* ``GET /views/{id}`` -> 200 view, 404, 400 invalid id
* ``PUT /views/{id}`` with a JSON ``{"name", "query"[, "kind"]}`` body -> 201 created,
  200 replaced (body: the stored view); 400 invalid id or body, 409 when the
  id is stored under another kind (a PUT without ``kind`` counts as ``trace``), 415 unless
  ``Content-Type: application/json``, 413 over :data:`MAX_BODY_BYTES`
* ``DELETE /views/{id}`` -> 204, 404, 400
* ``PUT`` and ``DELETE`` -> 403 ``{"error": "read_only_store: ...", "code": "read_only_store"}``
  when the views directory may not be written

Errors are ``{"error": "..."}``. The routes sit behind the OSPREY proxy like
the drafts routes and carry no auth of their own; as a CSRF guard, PUT and
DELETE answer 403 when ``Sec-Fetch-Site`` is present and is neither
``same-origin`` nor ``none``.
"""

import re
import json
import asyncio
import unicodedata
from typing import Any
from pathlib import Path
from datetime import UTC, datetime

from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from canopy_mcp.store import (
    ReadOnlyStore,
    lock_root,
    make_dirs,
    remove_file,
    write_atomic,
)

VIEW_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
VIEW_KIND_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
DEFAULT_KIND = "trace"
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


class ViewKindConflict(ValueError):
    """The id is already stored under a different kind."""


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


def check_kind(kind: Any) -> str:
    """``kind`` when it matches ``^[a-z][a-z0-9-]{0,31}$``."""
    if not isinstance(kind, str) or not VIEW_KIND_PATTERN.fullmatch(kind):
        raise InvalidView("kind must match ^[a-z][a-z0-9-]{0,31}$")
    return kind


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
    """Views stored as ``<views>/<id>.json``.

    ``views`` is ``views_dir`` when given, else ``<root>/views`` (``root`` being
    the drafts directory). It is created on the first write.
    """

    def __init__(self, root: Path | str, *, views_dir: Path | str | None = None):
        self.root = Path(root)
        self.views = Path(views_dir) if views_dir is not None else self.root / "views"
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
        kind = doc.get("kind", DEFAULT_KIND)
        if not isinstance(kind, str) or not VIEW_KIND_PATTERN.fullmatch(kind):
            return None
        return {"id": view_id, "name": name, "query": query, "kind": kind, "updated": doc["updated"]}

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

    async def put(self, view_id: str, name: Any, query: Any, kind: Any = None) -> tuple[dict[str, Any], bool]:
        """Create or replace the view; returns ``(view, created)``.

        An absent ``kind`` is ``trace``. Raises :class:`ViewKindConflict` when the id is
        already stored under another kind (checked inside the write lock).
        """
        path = self.path_for(view_id)
        kind = DEFAULT_KIND if kind is None else check_kind(kind)
        view = {
            "id": view_id,
            "name": clean_name(name),
            "query": check_query(query),
            "kind": kind,
            "updated": _now(),
        }
        data = _serialize(view)
        async with self._lock:
            created = await asyncio.to_thread(self._put_locked, view_id, path, kind, data)
        return view, created

    def _put_locked(self, view_id: str, path: Path, kind: str, data: bytes) -> bool:
        make_dirs(self.views)
        with lock_root(self.views):
            created = not path.exists()
            existing = None if created else self._load(path, view_id)
            if existing is not None and existing["kind"] != kind:
                raise ViewKindConflict(f"view {view_id!r} is stored as kind {existing['kind']!r}")
            write_atomic(path, data)
        return created

    async def delete(self, view_id: str) -> None:
        path = self.path_for(view_id)
        async with self._lock:
            await asyncio.to_thread(self._delete_locked, view_id, path)

    def _delete_locked(self, view_id: str, path: Path) -> None:
        try:
            # A views directory that was never created holds no view; deleting does not create it.
            with lock_root(self.views):
                remove_file(path)
        except FileNotFoundError:
            raise ViewNotFound(view_id) from None


# --- HTTP ----------------------------------------------------------------------


def _json(body: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": VIEWS_CACHE_CONTROL, **(headers or {})}
    )


def _error(status_code: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return _json({"error": message}, status_code, headers)


def _read_only() -> Response:
    # A fixed text: the exception names a server path, which a browser has no use for.
    message = f"{ReadOnlyStore.code}: saved views cannot be changed here"
    return _json({"error": message, "code": ReadOnlyStore.code}, 403)


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
        if not isinstance(body, dict) or not {"name", "query"} <= set(body) <= {"name", "query", "kind"}:
            return _error(400, 'body must be a JSON object with "name" and "query" and optionally "kind"')
        try:
            view, created = await views.put(view_id, body["name"], body["query"], body.get("kind"))
        except InvalidView as exc:
            return _error(400, str(exc))
        except ViewKindConflict as exc:
            return _error(409, str(exc))
        except ReadOnlyStore:
            return _read_only()
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
        except ReadOnlyStore:
            return _read_only()

    return [
        Route("/views", collection, methods=ALL_METHODS),
        # {path:path} so ids with a slash (always invalid) answer 400 here, not a bare 404.
        Route("/views/{view_id:path}", item, methods=ALL_METHODS),
    ]
