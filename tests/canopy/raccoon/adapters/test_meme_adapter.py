"""MemeDirectoryAdapter: thread dispatch, kwarg shaping, error mapping, health."""

import asyncio

import pytest

from canopy.raccoon.adapters.meme import MemeDirectoryAdapter
from canopy.raccoon.adapters.base import (
    InvalidQuery,
    DirectoryTimeout,
    DirectoryUnavailable,
)


class FakeNamesModule:
    """Stand-in for `meme.names`. Each method records its call and either
    returns a configured value or raises a configured exception."""

    def __init__(self) -> None:
        self.list_pvs_calls: list[tuple[tuple, dict]] = []
        self.list_devices_calls: list[tuple[tuple, dict]] = []
        self.list_elements_calls: list[tuple[tuple, dict]] = []
        self.return_value: list[str] = []
        self.raise_with: Exception | None = None

    def _maybe_raise(self):
        if self.raise_with is not None:
            raise self.raise_with

    def list_pvs(self, pattern: str, **kw):
        self.list_pvs_calls.append(((pattern,), kw))
        self._maybe_raise()
        return list(self.return_value)

    def list_devices(self, pattern: str, **kw):
        self.list_devices_calls.append(((pattern,), kw))
        self._maybe_raise()
        return list(self.return_value)

    def list_elements(self, pattern: str, **kw):
        self.list_elements_calls.append(((pattern,), kw))
        self._maybe_raise()
        return list(self.return_value)


@pytest.fixture
def fake_names() -> FakeNamesModule:
    return FakeNamesModule()


@pytest.fixture
def adapter(fake_names: FakeNamesModule) -> MemeDirectoryAdapter:
    return MemeDirectoryAdapter(names_module=fake_names)


@pytest.mark.asyncio
async def test_list_pvs_passes_kwargs_in_meme_form(adapter, fake_names) -> None:
    fake_names.return_value = ["A:1", "A:2"]

    out = await adapter.list_pvs("A%", tag="L1", sort_by="name", element_type="QUAD")

    assert out == ["A:1", "A:2"]
    assert fake_names.list_pvs_calls == [(("A%",), {"tag": "L1", "sort": "name", "element_type": "QUAD"})]


@pytest.mark.asyncio
async def test_list_pvs_omits_unset_kwargs(adapter, fake_names) -> None:
    fake_names.return_value = []
    await adapter.list_pvs("X")
    assert fake_names.list_pvs_calls == [(("X",), {})]


@pytest.mark.asyncio
async def test_list_devices_and_list_elements_dispatch_to_correct_function(adapter, fake_names) -> None:
    fake_names.return_value = ["DEV:1"]
    await adapter.list_devices("DEV")
    assert fake_names.list_devices_calls and not fake_names.list_pvs_calls

    await adapter.list_elements("EL")
    assert fake_names.list_elements_calls


@pytest.mark.asyncio
async def test_call_runs_in_thread_and_event_loop_stays_responsive(fake_names) -> None:
    """A synchronous `meme.names.list_pvs` runs in a thread, so the event loop
    keeps scheduling other coroutines while the call is in progress."""

    import time as time_module

    def slow_list_pvs(pattern, **kw):
        # Blocks the worker thread for ~50ms.
        time_module.sleep(0.05)
        return ["OK"]

    fake_names.list_pvs = slow_list_pvs  # type: ignore[assignment]
    adapter = MemeDirectoryAdapter(names_module=fake_names)

    counter = 0

    async def tick():
        nonlocal counter
        for _ in range(10):
            counter += 1
            await asyncio.sleep(0.005)

    out, _ = await asyncio.gather(adapter.list_pvs("X"), tick())
    assert out == ["OK"]
    # If the event loop had been blocked, `tick` would not have run while
    # `list_pvs` was sleeping in its thread.
    assert counter == 10


@pytest.mark.asyncio
async def test_timeout_error_maps_to_directory_timeout(adapter, fake_names) -> None:
    fake_names.raise_with = TimeoutError("upstream slow")
    with pytest.raises(DirectoryTimeout):
        await adapter.list_pvs("X")


@pytest.mark.asyncio
async def test_connection_error_maps_to_directory_unavailable(adapter, fake_names) -> None:
    fake_names.raise_with = ConnectionError("refused")
    with pytest.raises(DirectoryUnavailable):
        await adapter.list_pvs("X")


@pytest.mark.asyncio
async def test_os_error_maps_to_directory_unavailable(adapter, fake_names) -> None:
    fake_names.raise_with = OSError("network unreachable")
    with pytest.raises(DirectoryUnavailable):
        await adapter.list_pvs("X")


@pytest.mark.asyncio
async def test_value_error_maps_to_invalid_query(adapter, fake_names) -> None:
    fake_names.raise_with = ValueError("bad regex")
    with pytest.raises(InvalidQuery):
        await adapter.list_pvs("X")


@pytest.mark.asyncio
async def test_unexpected_error_maps_to_directory_unavailable(adapter, fake_names) -> None:
    fake_names.raise_with = RuntimeError("uncategorized")
    with pytest.raises(DirectoryUnavailable):
        await adapter.list_pvs("X")


@pytest.mark.asyncio
async def test_health_returns_healthy_on_round_trip(adapter, fake_names) -> None:
    fake_names.return_value = []
    h = await adapter.health()
    assert h.healthy is True
    assert h.last_error is None
    assert h.latency_ms is not None and h.latency_ms >= 0


@pytest.mark.asyncio
async def test_health_returns_unhealthy_on_error(adapter, fake_names) -> None:
    fake_names.raise_with = ConnectionError("down")
    h = await adapter.health()
    assert h.healthy is False
    assert h.last_error is not None


@pytest.mark.asyncio
async def test_close_is_no_op(adapter) -> None:
    await adapter.close()
