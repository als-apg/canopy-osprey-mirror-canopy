"""PVBroker keep-warm grace: a PV whose last subscriber left stays monitored for a while.

Within ``monitor_grace_seconds`` the monitor keeps running and the cache stays
current, so a client returning to a screen is answered from cache without a new
monitor or describe. After the grace period the PV is released as before.
"""

import asyncio

from tests.conftest import wait_for
from canopy.hare.config import HareConfig
from tests.canopy.hare.conftest import FakeWebSocket, settle
from canopy.hare.services.broker import PVBroker


def _count_calls(connector, method: str) -> list[str]:
    original = getattr(connector, method)
    calls: list[str] = []

    async def wrapped(pv, *args, **kwargs):
        calls.append(pv)
        return await original(pv, *args, **kwargs)

    setattr(connector, method, wrapped)
    return calls


def _initial_values(ws: FakeWebSocket, pv: str) -> list[dict]:
    return [m["data"][pv] for m in ws.sent if m["type"] == "initial" and pv in m["data"]]


async def test_resubscribe_within_grace_is_served_from_cache(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=5.0))
    monitors = _count_calls(fake_connector, "subscribe")
    describes = _count_calls(fake_connector, "describe")
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["LIVE:1"])
    await settle(broker)
    await broker.unsubscribe("c1", ["LIVE:1"])

    assert "LIVE:1" in fake_connector.callbacks
    assert "LIVE:1" in broker._latest
    assert broker.status()["warm_monitors"] == 1

    before = len(ws.sent)
    await broker.subscribe("c1", ["LIVE:1"])
    (initial,) = [m for m in ws.sent[before:] if m["type"] == "initial"]
    assert initial["data"]["LIVE:1"]["connected"] is True
    assert initial["meta"]["LIVE:1"]["units"] == "mm"
    assert monitors == ["LIVE:1"]
    assert describes == ["LIVE:1"]
    assert broker.status()["warm_monitors"] == 0
    await broker.stop()


async def test_monitor_values_keep_flowing_into_the_cache_during_grace(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=5.0))
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["LIVE:1"])
    await settle(broker)
    await broker.unsubscribe("c1", ["LIVE:1"])

    fake_connector.emit("LIVE:1", 7.0, ts=50)
    before = len(ws.sent)
    await settle(broker)
    # Nobody is subscribed, so nothing is sent ...
    assert ws.sent[before:] == []
    # ... but a returning subscriber gets the fresh value.
    await broker.subscribe("c1", ["LIVE:1"])
    assert _initial_values(ws, "LIVE:1")[-1]["value"] == 7.0
    await broker.stop()


async def test_a_pv_is_released_once_its_grace_has_passed(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=0.1))
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await settle(broker)
    await broker.unsubscribe("c1", ["LIVE:1"])

    await wait_for(lambda: "LIVE:1" not in fake_connector.callbacks, timeout=3.0)
    assert "LIVE:1" not in broker._latest
    assert "LIVE:1" not in broker._meta
    assert "LIVE:1" not in broker._seed_locks
    assert broker.status()["active_monitors"] == 0
    assert broker.status()["warm_monitors"] == 0


async def test_a_client_that_disconnects_leaves_its_pvs_warm(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=5.0))
    monitors = _count_calls(fake_connector, "subscribe")
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await settle(broker)

    await broker.remove_client("c1")
    assert "LIVE:1" in fake_connector.callbacks

    broker.add_client("c2", FakeWebSocket())
    await broker.subscribe("c2", ["LIVE:1"])
    assert monitors == ["LIVE:1"]
    await broker.stop()


async def test_resubscribe_cancels_the_pending_release(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=0.1))
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await settle(broker)
    await broker.unsubscribe("c1", ["LIVE:1"])
    await broker.subscribe("c1", ["LIVE:1"])

    await asyncio.sleep(0.25)
    assert "LIVE:1" in fake_connector.callbacks
    assert "LIVE:1" in broker._latest
    await broker.stop()


async def test_stop_closes_monitors_still_in_grace(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=60.0))
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])

    await broker.stop()
    assert "LIVE:1" not in fake_connector.callbacks
    assert not broker._grace


async def test_zero_grace_releases_at_once(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(monitor_grace_seconds=0))
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])
    assert "LIVE:1" not in fake_connector.callbacks
    assert not broker._grace


def test_the_default_keeps_monitors_warm_for_a_minute():
    assert HareConfig().monitor_grace_seconds == 60.0
