"""SingleFlight: concurrent callers coalesce, errors fan out, slot frees on completion."""

import asyncio

import pytest

from canopy.raccoon.cache.flight import SingleFlight


@pytest.mark.asyncio
async def test_concurrent_calls_coalesce_to_one_upstream() -> None:
    flight = SingleFlight()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ok"

    tasks = [asyncio.create_task(flight.run("k", factory)) for _ in range(100)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert results == ["ok"] * 100
    assert flight.in_flight_count() == 0


@pytest.mark.asyncio
async def test_exception_propagates_to_all_awaiters_and_frees_slot() -> None:
    flight = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    tasks = [asyncio.create_task(flight.run("k", factory)) for _ in range(5)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(results) == 5
    for r in results:
        assert isinstance(r, RuntimeError)
        assert str(r) == "boom"
    assert flight.in_flight_count() == 0


@pytest.mark.asyncio
async def test_subsequent_call_after_completion_starts_new_flight() -> None:
    flight = SingleFlight()
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await flight.run("k", factory) == 1
    assert await flight.run("k", factory) == 2
    assert flight.in_flight_count() == 0


@pytest.mark.asyncio
async def test_distinct_keys_run_independently() -> None:
    flight = SingleFlight()

    async def f_a() -> str:
        return "a"

    async def f_b() -> str:
        return "b"

    a, b = await asyncio.gather(flight.run("a", f_a), flight.run("b", f_b))
    assert (a, b) == ("a", "b")
