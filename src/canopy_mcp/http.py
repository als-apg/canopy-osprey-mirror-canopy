"""HTTP helpers the server's routes share: request bodies, the cross-site guard, JSON replies, ETags.

Each route keeps its own error envelope, since the viewer reads them as they are:
the API forward answers ``{"code", "detail"}`` (:func:`code_detail_response`), the
collections and workspace routes ``{"error": <code>, "detail"?}``
(:func:`error_response`), and the saved views ``{"error": <message>}`` (through
:func:`json_response`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from collections.abc import Callable

from starlette.requests import Request, HTTPConnection
from starlette.responses import Response, JSONResponse

__all__ = [
    "ALLOWED_FETCH_SITES",
    "ALL_METHODS",
    "NO_CACHE",
    "code_detail_response",
    "error_response",
    "etag",
    "is_cross_site",
    "is_cross_site_handshake",
    "LOOPBACK_HOSTS",
    "configured_origins",
    "host_allowed",
    "host_name",
    "normal_origin",
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


def is_cross_site(request: HTTPConnection) -> bool:
    """Whether ``Sec-Fetch-Site`` is present and not one of :data:`ALLOWED_FETCH_SITES`.

    A request without the header passes: only a browser sends it, and a browser
    always does, so this guards against cross-site pages, not against local clients.
    """
    site = request.headers.get("sec-fetch-site")
    return site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Origin schemes by URL scheme: a WebSocket URL names the origin of its HTTP twin.
_ORIGIN_SCHEMES = {"http": "http", "ws": "http", "https": "https", "wss": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}

Origin = tuple[str, str, int]


def host_name(value: str | None) -> str | None:
    """The lower-cased host name of a ``Host`` value (``name[:port]``, ``[v6][:port]``), or ``None``."""
    if not value or not value.strip():
        return None
    try:
        return urlsplit(f"//{value.strip()}").hostname
    except ValueError:
        return None


def normal_origin(url: str | None) -> Origin | None:
    """``(scheme, host, port)`` of ``url``: scheme http or https (ws/wss map to them),
    host lower-cased, port explicit or 80/443 by scheme. ``None`` when it is not one."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        scheme = _ORIGIN_SCHEMES.get(parts.scheme.lower())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme is None or not host:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def configured_origins(*urls: str | None) -> frozenset[Origin]:
    """The normalised origins of the configured server URLs that are set and valid."""
    return frozenset(o for o in map(normal_origin, urls) if o is not None)


def host_allowed(conn: HTTPConnection, configured: frozenset[Origin]) -> bool:
    """Whether ``Host`` names loopback or the host of a configured origin.

    ``configured`` holds the origins of the server's own URLs
    (:func:`configured_origins` of ``CANOPY_SELF_URL`` and ``CANOPY_PUBLIC_URL``).
    A missing or unparsable ``Host`` is not allowed. This is the DNS-rebinding
    defence: a rebound page's requests name the attacker's host.
    """
    name = host_name(conn.headers.get("host"))
    return name in LOOPBACK_HOSTS or (name is not None and name in {o[1] for o in configured})


def is_cross_site_handshake(conn: HTTPConnection, configured: frozenset[Origin]) -> bool:
    """Whether a WebSocket handshake must be refused as coming from another site.

    ``Host`` is already checked for every request by the app's ``HostGuard``
    (:func:`host_allowed`). The same-origin policy does not cover WebSockets,
    and under DNS rebinding a page's ``Origin`` and ``Host`` both name the
    attacker's host, so ``Origin`` is never compared with ``Host`` alone. Refused:

    - a cross-site ``Sec-Fetch-Site`` (:func:`is_cross_site`);
    - an ``Origin`` whose normalised origin (:func:`normal_origin`) is not a
      configured origin or, only when ``Host`` is loopback, the request's own
      origin (its scheme, the ``Host`` name and port). A loopback ``Origin`` is
      never accepted for a non-loopback ``Host``.

    A handshake without ``Origin`` (a non-browser client) is judged on
    ``Sec-Fetch-Site`` alone.
    """
    if is_cross_site(conn):
        return True
    origin = conn.headers.get("origin")
    if origin is None:
        return False
    host = conn.headers.get("host") or ""
    loopback = host_name(host) in LOOPBACK_HOSTS
    # A configured loopback origin (the default self URL) counts only on a loopback Host.
    allowed = {o for o in configured if loopback or o[1] not in LOOPBACK_HOSTS}
    if loopback:
        own = normal_origin(f"{conn.url.scheme}://{host.strip()}")
        if own is not None:
            allowed.add(own)
    seen = normal_origin(origin)
    return seen is None or seen not in allowed


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
