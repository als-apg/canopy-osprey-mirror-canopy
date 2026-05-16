"""EpicsArchiverApplianceAdapter — `fetch_window` (raw mode), `health`, `close`."""

import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from canopy.mole.adapters.base import (
    ArchiverTimeout,
    ArchiverBadResponse,
    ArchiverUnavailable,
    PVNotFoundInArchiver,
)
from canopy.mole.adapters.appliance import EpicsArchiverApplianceAdapter


@pytest.fixture
async def adapter():
    a = EpicsArchiverApplianceAdapter(
        name="test",
        url="https://archappl.example/retrieval",
        timeout_s=5.0,
    )
    yield a
    await a.close()


# --------------------------------------------------------------------------- #
# fetch_window happy paths
# --------------------------------------------------------------------------- #


async def test_fetch_window_parses_basic_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/data/getData\.json.*"),
        json=[
            {
                "meta": {"name": "PV:TEST", "EGU": "V", "PREC": "3"},
                "data": [
                    {"secs": 1000, "nanos": 500, "val": 1.5, "severity": 0, "status": 0},
                    {"secs": 1001, "nanos": 0, "val": 2.5, "severity": 1, "status": 0},
                ],
            }
        ],
    )
    samples = await adapter.fetch_window("PV:TEST", t0_ns=0, t1_ns=2_000_000_000_000)

    assert len(samples) == 2
    assert samples[0].ts_ns == 1000 * 1_000_000_000 + 500
    assert samples[0].value == 1.5
    assert samples[0].severity.name == "NO_ALARM"
    assert samples[1].severity.name == "MINOR"
    assert all(s.source == "archive" for s in samples)


async def test_fetch_window_url_and_params(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {}, "data": []}])
    await adapter.fetch_window(
        "PV:TEST",
        t0_ns=1_700_000_000_000_000_000,
        t1_ns=1_700_000_001_000_000_000,
    )
    request = httpx_mock.get_request()
    assert request.url.params["pv"] == "PV:TEST"
    assert request.url.params["donotchunk"] == "true"
    # ISO-8601 millisecond format with Z suffix.
    assert "T" in request.url.params["from"]
    assert request.url.params["from"].endswith("Z")


async def test_fetch_window_unknown_severity_becomes_invalid(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json=[
            {
                "meta": {},
                "data": [{"secs": 1, "nanos": 0, "val": 0, "severity": 99, "status": 0}],
            }
        ],
    )
    samples = await adapter.fetch_window("PV:TEST", t0_ns=0, t1_ns=10_000_000_000)
    assert samples[0].severity.name == "INVALID"


async def test_fetch_window_empty_top_level_returns_empty(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[])
    samples = await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)
    assert samples == []


async def test_fetch_window_default_nanos_is_zero(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {}, "data": [{"secs": 5, "val": 1.0, "severity": 0, "status": 0}]}])
    samples = await adapter.fetch_window("PV:TEST", 0, 10_000_000_000)
    assert samples[0].ts_ns == 5_000_000_000


# --------------------------------------------------------------------------- #
# fetch_window error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status_code", [400, 404])
async def test_fetch_window_unknown_pv_status(status_code, adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=status_code, content=b"unknown pv")
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window("PV:NOPE", 0, 1_000_000_000)


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_fetch_window_5xx_raises_unavailable(status_code, adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=status_code)
    with pytest.raises(ArchiverUnavailable):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_unexpected_4xx_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=418)
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_timeout_raises(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.TimeoutException("timed out"))
    with pytest.raises(ArchiverTimeout):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_transport_error_raises_unavailable(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    with pytest.raises(ArchiverUnavailable):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_malformed_json_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(content=b"<html>not json</html>")
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_missing_data_array_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {}}])  # no "data" key
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


async def test_fetch_window_malformed_row_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {}, "data": [{"missing": "secs_and_val"}]}])
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_window("PV:TEST", 0, 1_000_000_000)


# --------------------------------------------------------------------------- #
# Other contract methods
# --------------------------------------------------------------------------- #


async def test_health_success(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/mgmt/bpl/getApplianceInfo.*"),
        status_code=200,
        json={"identity": "test"},
    )
    h = await adapter.health()
    assert h.healthy is True
    assert h.last_error is None
    assert h.latency_ms is not None and h.latency_ms >= 0


async def test_health_5xx_unhealthy(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=re.compile(r".*/mgmt/bpl/getApplianceInfo.*"), status_code=503)
    h = await adapter.health()
    assert h.healthy is False
    assert "503" in (h.last_error or "")


async def test_health_transport_error_unhealthy(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    h = await adapter.health()
    assert h.healthy is False
    assert h.last_error is not None


async def test_close_idempotent():
    a = EpicsArchiverApplianceAdapter(name="x", url="https://x.example/r")
    await a.close()
    await a.close()  # second call must not raise
