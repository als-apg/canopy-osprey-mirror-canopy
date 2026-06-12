"""Tests for Vole's ChangeNotifier pub/sub mechanism."""

import asyncio

import pytest

from canopy.vole.services.notifier import ChangeNotifier


@pytest.fixture
def notifier():
    return ChangeNotifier()


class TestChangeNotifier:
    async def test_subscribe_and_notify(self, notifier):
        received = []

        async def cb(pv: str) -> None:
            received.append(pv)

        notifier.subscribe("PV:A", cb)
        notifier.notify("PV:A")
        await asyncio.sleep(0)

        assert received == ["PV:A"]

    async def test_multiple_subscribers_same_pv(self, notifier):
        received_1 = []
        received_2 = []

        async def cb1(pv: str) -> None:
            received_1.append(pv)

        async def cb2(pv: str) -> None:
            received_2.append(pv)

        notifier.subscribe("PV:X", cb1)
        notifier.subscribe("PV:X", cb2)
        notifier.notify("PV:X")
        await asyncio.sleep(0)

        assert received_1 == ["PV:X"]
        assert received_2 == ["PV:X"]

    async def test_unsubscribe_stops_notifications(self, notifier):
        received = []

        async def cb(pv: str) -> None:
            received.append(pv)

        unsub = notifier.subscribe("PV:A", cb)
        notifier.notify("PV:A")
        await asyncio.sleep(0)
        assert received == ["PV:A"]

        unsub()
        notifier.notify("PV:A")
        await asyncio.sleep(0)
        assert received == ["PV:A"]

    async def test_notify_unknown_pv_is_noop(self, notifier):
        notifier.notify("NONEXISTENT")
        await asyncio.sleep(0)

    async def test_callback_error_is_logged_not_raised(self, notifier):
        good_received = []

        async def bad_cb(pv: str) -> None:
            raise RuntimeError("boom")

        async def good_cb(pv: str) -> None:
            good_received.append(pv)

        notifier.subscribe("PV:A", bad_cb)
        notifier.subscribe("PV:A", good_cb)
        notifier.notify("PV:A")
        await asyncio.sleep(0)

        assert good_received == ["PV:A"]

    async def test_unsubscribe_cleans_up_empty_pv(self, notifier):
        async def cb(pv: str) -> None:
            pass

        unsub = notifier.subscribe("PV:A", cb)
        assert "PV:A" in notifier._listeners

        unsub()
        assert "PV:A" not in notifier._listeners

    async def test_notify_does_not_block(self, notifier):
        """Notify returns immediately; callbacks run asynchronously."""
        called = asyncio.Event()

        async def slow_cb(pv: str) -> None:
            await asyncio.sleep(0.1)
            called.set()

        notifier.subscribe("PV:A", slow_cb)
        notifier.notify("PV:A")
        assert not called.is_set()
        await asyncio.wait_for(called.wait(), timeout=1.0)
