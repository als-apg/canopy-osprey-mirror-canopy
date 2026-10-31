"""read_pv and search_pvs against a mocked canopy backend (pytest-httpx)."""

import httpx
import pytest
from pytest_httpx import HTTPXMock
from fastmcp.exceptions import ToolError

from canopy_mcp.client import API_KEY_HEADER
from canopy_mcp.config import get_settings
from canopy_mcp.tools.pvs import read_pv, search_pvs

BASE = "http://canopy-api:8000"


@pytest.fixture(autouse=True)
def backend_env(monkeypatch):
    for name in ("CANOPY_DRAFTS_DIR", "CANOPY_VIEWER_DIST", "CANOPY_PANEL_ID", "CANOPY_PV_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CANOPY_BASE_URL", BASE)
    monkeypatch.setenv("CANOPY_API_KEY", "read-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


PV_VALUE = {
    "pv": "SR:C01:BPM1:X",
    "value": 0.125,
    "valueKind": "double",
    "severity": 0,
    "tsNs": 1,
    "connected": True,
    "error": None,
}
PV_META = {"pv": "SR:C01:BPM1:X", "valueKind": "double", "units": "mm", "precision": 3, "description": "BPM x"}


# --- read_pv ------------------------------------------------------------------


async def test_read_pv_returns_value(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=f"{BASE}/hare/v1/pvs/SR:C01:BPM1:X", json=PV_VALUE)

    result = await read_pv("SR:C01:BPM1:X")

    assert result["value"] == PV_VALUE
    assert "describe" not in result
    [request] = httpx_mock.get_requests()
    assert request.headers[API_KEY_HEADER] == "read-key"
    assert "read-key" not in str(request.url)


async def test_read_pv_with_describe_adds_metadata(httpx_mock: HTTPXMock):
    httpx_mock.add_response(method="GET", url=f"{BASE}/hare/v1/pvs/SR:C01:BPM1:X", json=PV_VALUE)
    httpx_mock.add_response(method="GET", url=f"{BASE}/hare/v1/pvs/SR:C01:BPM1:X/describe", json=PV_META)

    result = await read_pv("SR:C01:BPM1:X", describe=True)

    assert result["value"] == PV_VALUE
    assert result["describe"] == PV_META
    assert all(r.method == "GET" for r in httpx_mock.get_requests())


async def test_read_pv_keeps_protocol_prefix_in_path(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=f"{BASE}/hare/v1/pvs/pva://SR:C01:BPM1:X", json=PV_VALUE)

    result = await read_pv("pva://SR:C01:BPM1:X")

    assert result["value"]["value"] == 0.125


async def test_read_pv_error_raises_tool_error_naming_status(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=404, json={"code": "pv_not_found", "detail": "no such PV"})

    with pytest.raises(ToolError, match="404|pv_not_found"):
        await read_pv("NOT:A:PV")


async def test_read_pv_timeout_raises_tool_error(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

    with pytest.raises(ToolError):
        await read_pv("SR:C01:BPM1:X")


# --- search_pvs ---------------------------------------------------------------


async def test_search_pvs_returns_names(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="GET",
        url=f"{BASE}/raccoon/v1/pvs?pattern=SR%3AC01%3A%2A",
        json={"names": ["SR:C01:BPM1:X", "SR:C01:BPM1:Y"]},
    )

    result = await search_pvs("SR:C01:*")

    assert result == {"names": ["SR:C01:BPM1:X", "SR:C01:BPM1:Y"], "skipped": []}
    [request] = httpx_mock.get_requests()
    assert request.headers[API_KEY_HEADER] == "read-key"


async def test_search_pvs_adapter_misconfigured_is_skipped_not_raised(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        status_code=500, json={"code": "adapter_misconfigured", "detail": "no directory adapter configured"}
    )

    result = await search_pvs("SR:*")

    assert result["names"] == []
    [skip] = result["skipped"]
    assert skip["check"] == "raccoon"
    assert "adapter_misconfigured" in skip["reason"]


async def test_search_pvs_4xx_is_skipped_with_status(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=400, json={"code": "invalid_query", "detail": "bad pattern"})

    result = await search_pvs("[")

    assert result["names"] == []
    [skip] = result["skipped"]
    assert skip["check"] == "raccoon"
    assert "400" in skip["reason"] or "invalid_query" in skip["reason"]


async def test_search_pvs_non_json_error_reports_status(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=502, text="<html>bad gateway</html>")

    result = await search_pvs("SR:*")

    assert result == {"names": [], "skipped": [{"check": "raccoon", "reason": "502"}]}


async def test_search_pvs_timeout_is_skipped(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))

    result = await search_pvs("SR:*")

    assert result["names"] == []
    [skip] = result["skipped"]
    assert skip["check"] == "raccoon"
    assert "timeout" in skip["reason"].lower() or "timed out" in skip["reason"]


async def test_search_pvs_connection_error_is_skipped(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    result = await search_pvs("SR:*")

    assert result["names"] == []
    assert result["skipped"][0]["check"] == "raccoon"
    assert "connection refused" in result["skipped"][0]["reason"]


async def test_search_pvs_malformed_body_is_skipped(httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=200, text="not json")

    result = await search_pvs("SR:*")

    assert result["names"] == []
    assert result["skipped"][0]["check"] == "raccoon"


def test_search_pvs_description_tells_agent_to_confirm_with_read_pv():
    assert "read_pv" in (search_pvs.__doc__ or "")
