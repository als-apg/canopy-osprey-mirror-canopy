"""PV tools: read one PV through Hare and search PV names through Raccoon.

Both are read-only; nothing here ever writes a PV. ``search_pvs`` never raises:
a directory outage must not stop the agent from building a panel, so any
failure comes back as an entry in ``skipped`` in the same shape the lint
tool uses.
"""

from typing import Any
from urllib.parse import quote

import httpx
from fastmcp.exceptions import ToolError

from canopy_mcp.server import mcp
from canopy_mcp.client import make_client, error_reason

TIMEOUT_S = 10.0


def _error_message(response: httpx.Response) -> str:
    """``HTTP <status>`` plus the canopy error envelope's ``code`` when there is one."""
    reason = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return reason
    if isinstance(body, dict) and body.get("code"):
        reason = f"{reason} {body['code']}"
        if body.get("detail"):
            reason = f"{reason}: {body['detail']}"
    return reason


def _pv_path(name: str) -> str:
    # Hare's route uses a path converter, so slashes (``pva://``) stay literal.
    return "/hare/v1/pvs/" + quote(name, safe="/:")


async def _get_json(client: httpx.AsyncClient, path: str) -> Any:
    try:
        response = await client.get(path)
    except httpx.HTTPError as exc:
        raise ToolError(f"hare request failed: {type(exc).__name__}: {exc}") from exc
    if not response.is_success:
        raise ToolError(f"hare: {_error_message(response)}")
    return response.json()


@mcp.tool
async def read_pv(name: str, describe: bool = False) -> dict[str, Any]:
    """Read the current value of one PV (read-only; never writes).

    Returns ``{"value": {pv, value, valueKind, severity, tsNs, connected, error}}``;
    with ``describe=true`` also ``"describe"``: units, precision, description and
    enum choices. Use this to confirm that a PV name you guessed or found with
    ``search_pvs`` actually exists and connects before putting it in a screen.
    """
    async with make_client(timeout=TIMEOUT_S) as client:
        result: dict[str, Any] = {"value": await _get_json(client, _pv_path(name))}
        if describe:
            result["describe"] = await _get_json(client, _pv_path(name) + "/describe")
    return result


@mcp.tool
async def search_pvs(pattern: str) -> dict[str, Any]:
    """Search the PV directory (Raccoon) for names matching a glob-style pattern, e.g. ``SR:C01:*``.

    Returns ``{"names": [...], "skipped": [...]}``. If the directory is unavailable
    the call does not fail: ``names`` is empty and ``skipped`` says why, so an empty
    result is not proof a PV does not exist. A name from here, or one you guessed,
    should be confirmed with ``read_pv`` before you use it.
    """
    try:
        async with make_client(timeout=TIMEOUT_S) as client:
            response = await client.get("/raccoon/v1/pvs", params={"pattern": pattern})
    except httpx.HTTPError as exc:
        return _skipped(f"{type(exc).__name__}: {exc}")
    if not response.is_success:
        return _skipped(error_reason(response))
    try:
        names = response.json()["names"]
    except (ValueError, KeyError, TypeError):
        return _skipped(f"HTTP {response.status_code}: malformed response body")
    if not isinstance(names, list):
        return _skipped(f"HTTP {response.status_code}: malformed response body")
    return {"names": [str(n) for n in names], "skipped": []}


def _skipped(reason: str) -> dict[str, Any]:
    return {"names": [], "skipped": [{"check": "raccoon", "reason": reason}]}
