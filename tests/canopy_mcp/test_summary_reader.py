"""One reading of DATA's summary in flight per app, shared by ``config.json`` and ``/health/summary``.

Every caller who asks while a request for the summary is in flight gets that
request's answer; the next caller after it lands sends a new one. The app is
driven on the test's own event loop so the callers really overlap.
"""

import time
import asyncio
from pathlib import Path
from collections.abc import Callable, Awaitable

import httpx
import pytest

from canopy_mcp import static
from canopy_mcp.app import create_app
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from tests.canopy_mcp.conftest import summary

pytestmark = pytest.mark.real_data_summary

Respond = Callable[[httpx.Request], Awaitable[httpx.Response]]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(drafts_dir=tmp_path / "drafts", pv_mode="hare", api_key="sekrit-key")


class Data:
    """A DATA double: records every request and answers with ``respond``."""

    def __init__(self, respond: Respond) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.respond(request)


def _json(body: dict, status: int = 200) -> Respond:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return respond


def _held(gate: asyncio.Event, body: dict) -> Respond:
    """Answers ``body`` once ``gate`` is set."""

    async def respond(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, json=body)

    return respond


def _front(settings: Settings, data: Data) -> httpx.AsyncClient:
    """A client for a new app whose shared client reaches ``data``."""
    app = create_app(settings, startup_hooks=[])
    app.state.http = make_client(settings, transport=httpx.MockTransport(data))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")


async def test_concurrent_callers_of_both_routes_share_one_request(settings: Settings) -> None:
    gate = asyncio.Event()
    data = Data(_held(gate, summary(binding="va@3", connected=2, active_monitors=3)))
    async with _front(settings, data) as front:
        paths = ["/config.json"] * 5 + ["/health/summary"] * 5
        callers = [asyncio.ensure_future(front.get(path)) for path in paths]
        await asyncio.sleep(0.1)
        assert len(data.requests) == 1
        gate.set()
        answers = await asyncio.gather(*callers)
    assert len(data.requests) == 1
    assert [r.status_code for r in answers] == [200] * 10
    for answer in answers[:5]:
        assert answer.json()["binding"] == "va@3"
    for answer in answers[5:]:
        assert answer.json()["channels"] == {"connected": 2, "total": 3}


async def test_a_reading_is_never_reused_after_it_lands(settings: Settings) -> None:
    bindings = iter(["va@3", "va@4"])

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=summary(binding=next(bindings)))

    data = Data(respond)
    async with _front(settings, data) as front:
        first = await front.get("/config.json")
        second = await front.get("/config.json")
    assert len(data.requests) == 2
    assert first.json()["binding"] == "va@3"
    assert second.json()["binding"] == "va@4"


async def test_a_caller_that_goes_away_leaves_the_reading_to_the_others(settings: Settings) -> None:
    gate = asyncio.Event()
    data = Data(_held(gate, summary(binding="va@3")))
    async with _front(settings, data) as front:
        leaver = asyncio.ensure_future(front.get("/config.json"))
        stayer = asyncio.ensure_future(front.get("/config.json"))
        await asyncio.sleep(0.1)
        leaver.cancel()
        await asyncio.gather(leaver, return_exceptions=True)
        gate.set()
        answer = await stayer
    assert leaver.cancelled()
    assert answer.json()["binding"] == "va@3"
    assert len(data.requests) == 1


async def test_a_hanging_data_answers_every_waiter_within_the_bound(settings: Settings) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    data = Data(hang)
    async with _front(settings, data) as front:
        start = time.monotonic()
        answers = await asyncio.gather(*(front.get("/config.json") for _ in range(4)))
        elapsed = time.monotonic() - start
    assert [r.json()["backend"] for r in answers] == [None] * 4
    assert len(data.requests) == 1
    assert elapsed < static.SUMMARY_TIMEOUT_S + 1.0


async def test_the_reader_is_per_app(settings: Settings) -> None:
    gate = asyncio.Event()
    data = Data(_held(gate, summary(binding="va@3")))
    async with _front(settings, data) as one, _front(settings, data) as other:
        callers = [asyncio.ensure_future(front.get("/config.json")) for front in (one, other)]
        await asyncio.sleep(0.1)
        assert len(data.requests) == 2
        gate.set()
        answers = await asyncio.gather(*callers)
    assert [r.json()["binding"] for r in answers] == ["va@3", "va@3"]
