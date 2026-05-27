"""EpicsArchiverApplianceAdapter — `metadata` and `search`."""

import re

import pytest
from pytest_httpx import HTTPXMock

from canopy.mole.adapters.base import (
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
# metadata
# --------------------------------------------------------------------------- #


async def test_metadata_pulls_egu_and_prec_from_meta_block(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json=[{"meta": {"name": "PV:TEST", "EGU": "DegC", "PREC": "2"}, "data": []}],
    )
    md = await adapter.metadata("PV:TEST")
    assert md.pv == "PV:TEST"
    assert md.archiver == "test"
    assert md.units == "DegC"
    assert md.precision == 2
    # Fields the Appliance doesn't carry.
    assert md.description is None
    assert md.alarm_low is None
    assert md.display_high is None


async def test_metadata_handles_missing_egu_prec(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {}, "data": []}])
    md = await adapter.metadata("PV:TEST")
    assert md.units is None
    assert md.precision is None


async def test_metadata_non_int_prec_becomes_none(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[{"meta": {"PREC": "not-a-number"}, "data": []}])
    md = await adapter.metadata("PV:TEST")
    assert md.precision is None


async def test_metadata_empty_response_raises_pv_not_found(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(json=[])
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.metadata("PV:NOPE")


async def test_metadata_400_raises_pv_not_found(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(status_code=400)
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.metadata("PV:NOPE")


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


async def test_search_splits_whitespace_separated_names(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"),
        text="PV:A\nPV:B PV:C\n  PV:D ",
    )
    pvs = await adapter.search("PV:.*", limit=10)
    assert pvs == ["PV:A", "PV:B", "PV:C", "PV:D"]


async def test_search_applies_limit(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"),
        text="\n".join(f"PV:{i}" for i in range(20)),
    )
    pvs = await adapter.search("PV:.*", limit=5)
    assert pvs == [f"PV:{i}" for i in range(5)]


async def test_search_passes_pattern_as_regex_param(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"),
        text="",
    )
    await adapter.search("MY_PATTERN.*", limit=10)
    request = httpx_mock.get_request()
    assert request.url.params["regex"] == "MY_PATTERN.*"


async def test_search_empty_response_returns_empty_list(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"), text="")
    assert await adapter.search("PV:.*", limit=10) == []


async def test_search_5xx_raises_unavailable(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"),
        status_code=503,
    )
    with pytest.raises(ArchiverUnavailable):
        await adapter.search("PV:.*", limit=10)


async def test_search_unexpected_4xx_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=re.compile(r".*/retrieval/bpl/searchForPVsRegex.*"),
        status_code=418,
    )
    with pytest.raises(ArchiverBadResponse):
        await adapter.search("PV:.*", limit=10)
