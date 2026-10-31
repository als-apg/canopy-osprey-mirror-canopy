"""HTTP helpers the server's routes share: request bodies, the cross-site guard, JSON replies, ETags.

Each route keeps its own error envelope, since the viewer reads them as they are:
the API forward answers ``{"code", "detail"}`` (:func:`code_detail_response`), the
collections and workspace routes ``{"error": <code>, "detail"?}``
(:func:`error_response`), and the saved views ``{"error": <message>}`` (through
:func:`json_response`).
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import Response, JSONResponse

__all__ = [
    "ALLOWED_FETCH_SITES",
    "ALL_METHODS",
    "NO_CACHE",
    "code_detail_response",
    "error_response",
    "etag",
    "is_cross_site",
    "is_json_request",
    "json_response",
    "not_modified",
    "read_capped_body",
    "refuse_cross_site",
]

# Every method, for routes that answer their own 405: Starlette maps ``methods=None``
# to GET only, so a route that lists these sees every request and can say what it allows.
ALL_METHODS = ["GET", "HEAD", "PUT", "DELETE", "POST", "PATCH", "OPTIONS"]
# Sec-Fetch-Site values a guarded request may carry; any other value is a cross-site request.
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
NO_CACHE = "no-cache"


async def read_capped_body(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` when it is over ``cap`` bytes.

    A ``Content-Length`` above ``cap``, or one that is not an integer, is refused
    before anything is read; the stream is then read chunk by chunk and given up
    as soon as it passes ``cap``, whatever the header said.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def is_json_request(request: Request) -> bool:
    """Whether the request's media type is ``application/json`` (parameters ignored)."""
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media == "application/json"


def is_cross_site(request: Request) -> bool:
    """Whether ``Sec-Fetch-Site`` is present and not one of :data:`ALLOWED_FETCH_SITES`.

    A request without the header passes: only a browser sends it, and a browser
    always does, so this guards against cross-site pages, not against local clients.
    """
    site = request.headers.get("sec-fetch-site")
    return site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES


def refuse_cross_site(request: Request, refusal: Callable[[], Response]) -> Response | None:
    """``refusal()`` for a cross-site request (:func:`is_cross_site`), else ``None``."""
    return refusal() if is_cross_site(request) else None


def json_response(
    body: Any, status_code: int = 200, headers: dict[str, str] | None = None, *, cache_control: str | None = NO_CACHE
) -> Response:
    """A JSON response; ``Cache-Control`` is ``no-cache`` unless ``cache_control`` says otherwise (``None``: none)."""
    merged = {} if cache_control is None else {"Cache-Control": cache_control}
    merged.update(headers or {})
    return JSONResponse(body, status_code=status_code, headers=merged)


def error_response(
    status_code: int, code: str, detail: str | None = None, headers: dict[str, str] | None = None
) -> Response:
    """``{"error": code, "detail"?: detail}`` with ``Cache-Control: no-cache``."""
    body = {"error": code} if detail is None else {"error": code, "detail": detail}
    return json_response(body, status_code, headers)


def code_detail_response(status_code: int, code: str, detail: str) -> Response:
    """``{"code": code, "detail": detail}``, the canopy API's own error shape, without cache headers."""
    return JSONResponse({"code": str(code), "detail": detail}, status_code=status_code)


def etag(rev: str) -> str:
    """The strong ETag for ``rev``: the rev in double quotes."""
    return f'"{rev}"'


def not_modified(request: Request, tag: str) -> bool:
    """Whether ``If-None-Match`` names ``tag`` (weak and list forms match) or is ``*``."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return tag in candidates or "*" in candidates
