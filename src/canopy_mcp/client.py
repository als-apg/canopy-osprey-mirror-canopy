"""The single factory for HTTP clients that talk to the canopy backend.

Every tool goes through :func:`make_client` so the base URL and the
``X-API-Key`` header come from one place, and a key never leaks into a URL;
the WebSocket relays take the same header from :func:`auth_headers`.
"""

from typing import Any
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx

from canopy_mcp.config import Settings, get_settings

API_KEY_HEADER = "X-API-Key"


def error_reason(response: httpx.Response) -> str:
    """The canopy ``{code, detail}`` error code, else the HTTP status.

    The ``reason`` of a ``{check, reason}`` entry in a ``skipped`` list, so a
    backend outage reads identically wherever it is reported.
    """
    try:
        code = response.json().get("code")
    except Exception:
        code = None
    return code if isinstance(code, str) and code else str(response.status_code)


def auth_headers(settings: Settings) -> dict[str, str]:
    """The ``X-API-Key`` header for ``settings.api_key``; empty when no key is set.

    ``settings.api_key`` already holds the key from ``CANOPY_API_KEY_FILE``
    when that file is configured, so the file's key is what goes upstream.
    """
    return {API_KEY_HEADER: settings.api_key} if settings.api_key else {}


def make_client(settings: Settings | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` bound to the canopy backend.

    Extra keyword arguments are passed through to ``httpx.AsyncClient``
    (e.g. ``timeout`` or a test ``transport``); caller-supplied headers are
    merged over the API-key header.
    """
    settings = settings or get_settings()
    headers = auth_headers(settings)
    headers.update(kwargs.pop("headers", None) or {})
    return httpx.AsyncClient(base_url=settings.base_url, headers=headers, **kwargs)


@asynccontextmanager
async def borrowed_client(
    client: httpx.AsyncClient | None, settings: Settings | None = None, **kwargs: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """``client`` when one is given (left open), else a :func:`make_client` client closed on exit."""
    if client is not None:
        yield client
        return
    async with make_client(settings, **kwargs) as owned:
        yield owned
