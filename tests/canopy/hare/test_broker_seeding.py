"""Subscribe seeding: no head-of-line blocking, negative cache, keep-warm grace.

A subscribe seeds each newly-activated PV with a one-shot get + describe. An
unreachable name only answers after the connector's timeout, so:

- live PVs' first values must not wait for a dead one, neither inside a batch
  nor across subscribe frames on one socket;
- a name that just failed to resolve is not looked up again within its TTL;
- a PV released by its last subscriber stays monitored for a grace period, so
  switching back to a screen is served from cache.
"""

import time
import asyncio
from dataclasses import field, dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, PVValue, ValueKind
from canopy.hare.api.v1 import ws as ws_module
from canopy.core.security import ws_require_read_access
from canopy.contracts.sample import AlarmSeverity
from tests.canopy.hare.conftest import FakeWebSocket, FakeConnector
from canopy.hare.dependencies import get_pv_broker
from canopy.hare.services.broker import PVBroker

DEAD_DELAY_S = 0.5


@dataclass
class SlowConnector(FakeConnector):
    """FakeConnector whose reads take time; names in ``dead`` never resolve.

    A dead name answers like the EPICS backend does after its timeout: a
    disconnected value from ``get`` and bare metadata from ``describe``.
    """

    dead: set[str] = field(default_factory=set)
    get_delay_s: float = 0.0
    describe_delay_s: float = 0.0
    gets: list[str] = field(default_factory=list)
    describes: list[str] = field(default_factory=list)
    monitors_started: list[str] = field(default_factory=list)

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        self.gets.append(pv)
        if pv in self.dead:
            await asyncio.sleep(DEAD_DELAY_S)
            return PVValue(pv=pv, value=None, severity=AlarmSeverity.INVALID, ts_ns=1, connected=False, error="timeout")
        await asyncio.sleep(self.get_delay_s)
        return PVValue(pv=pv, value=1.0, severity=AlarmSeverity.NO_ALARM, ts_ns=1, connected=True)

    async def describe(self, pv: str) -> PVMeta:
        self.describes.append(pv)
        if pv in self.dead:
            await asyncio.sleep(DEAD_DELAY_S)
            return PVMeta(pv=pv)
        await asyncio.sleep(self.describe_delay_s)
        return PVMeta(pv=pv, value_kind=ValueKind.SCALAR, units="mm", precision=3)

    async def subscribe(self, pv, on_event):
        self.monitors_started.append(pv)
        return await super().subscribe(pv, on_event)


def _config(**overrides) -> HareConfig:
    base = {
        "ws_batch_interval_ms": 10,
        "heartbeat_interval_s": 100.0,
        "monitor_grace_seconds": 0.0,
        "negative_cache_ttl_s": 30.0,
    }
    base.update(overrides)
    return HareConfig(**base)


class _TimedSocket(FakeWebSocket):
    """Records when each PV first appears in an ``initial`` frame."""

    def __init__(self) -> None:
        super().__init__()
        self.first_initial: dict[str, float] = {}

    async def send_json(self, data) -> None:
        await super().send_json(data)
        if data.get("type") == "initial":
            now = time.monotonic()
            for pv in data["data"]:
                self.first_initial.setdefault(pv, now)

    def initial_values(self, pv: str) -> list[dict]:
        return [m["data"][pv] for m in self.sent if m["type"] == "initial" and pv in m["data"]]


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --- head-of-line blocking -------------------------------------------------- #


async def test_dead_pv_in_a_batch_does_not_delay_live_first_values():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    t0 = time.monotonic()
    await broker.subscribe("c1", ["DEAD:1"] + [f"LIVE:{i}" for i in range(20)])

    live = [ws.first_initial[f"LIVE:{i}"] - t0 for i in range(20)]
    assert max(live) < DEAD_DELAY_S / 2
    # The dead name still gets its (disconnected) initial once it times out.
    assert ws.first_initial["DEAD:1"] - t0 >= DEAD_DELAY_S * 0.9
    assert ws.initial_values("DEAD:1")[0]["connected"] is False


async def test_subscribe_without_waiting_returns_before_seeding_finishes():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    t0 = time.monotonic()
    accepted = await broker.subscribe("c1", ["DEAD:1"], wait_seed=False)
    assert accepted == ["DEAD:1"]
    assert time.monotonic() - t0 < DEAD_DELAY_S / 2

    # A later frame on the same socket is answered before the dead one.
    await broker.subscribe("c1", ["LIVE:1"], wait_seed=False)
    await _wait_for(lambda: "LIVE:1" in ws.first_initial)
    assert "DEAD:1" not in ws.first_initial
    await _wait_for(lambda: "DEAD:1" in ws.first_initial)
    await broker.stop()


async def test_get_and_describe_run_concurrently():
    connector = SlowConnector(get_delay_s=0.3, describe_delay_s=0.3)
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    t0 = time.monotonic()
    await broker.subscribe("c1", ["LIVE:1"])
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5  # sequential would be >= 0.6
    initial = [m for m in ws.sent if m["type"] == "initial"][0]
    assert "LIVE:1" in initial["data"] and "LIVE:1" in initial["meta"]


async def test_seed_does_not_overwrite_a_newer_monitor_value():
    connector = SlowConnector(get_delay_s=0.2)
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    task = asyncio.create_task(broker.subscribe("c1", ["LIVE:1"]))
    await _wait_for(lambda: "LIVE:1" in connector.callbacks)
    connector.emit("LIVE:1", 42.0, ts=99)  # monitor tick lands before the get
    await broker._drain_nowait()
    await task

    assert ws.initial_values("LIVE:1")[-1]["value"] == 42.0
    assert broker._latest["LIVE:1"].value == 42.0


async def test_concurrent_subscribers_share_one_seed_and_both_get_an_initial():
    connector = SlowConnector(get_delay_s=0.2)
    broker = PVBroker(connector, _config())
    ws1, ws2 = _TimedSocket(), _TimedSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)

    await asyncio.gather(broker.subscribe("c1", ["LIVE:1"]), broker.subscribe("c2", ["LIVE:1"]))

    assert connector.gets.count("LIVE:1") == 1
    assert "LIVE:1" in ws1.first_initial
    assert "LIVE:1" in ws2.first_initial


async def test_no_initial_for_a_pv_unsubscribed_before_its_seed_lands():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["DEAD:1"], wait_seed=False)
    await broker.unsubscribe("c1", ["DEAD:1"])
    await asyncio.sleep(DEAD_DELAY_S * 1.5)

    assert "DEAD:1" not in ws.first_initial
    await broker.stop()


async def test_remove_client_cancels_its_pending_deliveries():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = _TimedSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["DEAD:1"], wait_seed=False)

    await broker.remove_client("c1")
    await asyncio.sleep(0)
    assert not broker._deliveries.get("c1")
    await broker.stop()


async def test_seed_concurrency_is_bounded_across_the_broker():
    in_flight = 0
    peak = 0

    @dataclass
    class Counting(SlowConnector):
        async def get(self, pv, *, timeout=None):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.02)
                return await super().get(pv, timeout=timeout)
            finally:
                in_flight -= 1

    broker = PVBroker(Counting(), _config(seed_concurrency=4))
    for c in ("c1", "c2"):
        broker.add_client(c, _TimedSocket())
    await asyncio.gather(
        broker.subscribe("c1", [f"A:{i}" for i in range(20)]),
        broker.subscribe("c2", [f"B:{i}" for i in range(20)]),
    )
    assert peak <= 4


# --- negative cache ---------------------------------------------------------- #


async def test_dead_name_is_not_looked_up_again_within_its_ttl():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config(negative_cache_ttl_s=30.0))
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["DEAD:1"])
    await broker.unsubscribe("c1", ["DEAD:1"])
    assert "DEAD:1" not in broker._latest  # released (no grace)

    t0 = time.monotonic()
    await broker.subscribe("c1", ["DEAD:1"])
    assert time.monotonic() - t0 < DEAD_DELAY_S / 2
    assert connector.gets.count("DEAD:1") == 1
    assert connector.describes.count("DEAD:1") == 1
    # The client is still told the name is disconnected, straight away.
    assert [v["connected"] for v in ws.initial_values("DEAD:1")] == [False, False]


async def test_negative_cache_entry_expires():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config(negative_cache_ttl_s=0.1))
    broker.add_client("c1", _TimedSocket())

    await broker.subscribe("c1", ["DEAD:1"])
    await broker.unsubscribe("c1", ["DEAD:1"])
    await asyncio.sleep(0.15)
    await broker.subscribe("c1", ["DEAD:1"])

    assert connector.gets.count("DEAD:1") == 2


async def test_connected_monitor_event_clears_the_negative_entry():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config(negative_cache_ttl_s=30.0))
    broker.add_client("c1", _TimedSocket())

    await broker.subscribe("c1", ["DEAD:1"])
    connector.emit("DEAD:1", 5.0)  # the IOC came up
    await broker._drain_nowait()
    await broker.unsubscribe("c1", ["DEAD:1"])
    await broker.subscribe("c1", ["DEAD:1"])

    assert connector.gets.count("DEAD:1") == 2


async def test_negative_cache_ttl_zero_disables_it():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config(negative_cache_ttl_s=0.0))
    broker.add_client("c1", _TimedSocket())

    await broker.subscribe("c1", ["DEAD:1"])
    await broker.unsubscribe("c1", ["DEAD:1"])
    await broker.subscribe("c1", ["DEAD:1"])
    assert connector.gets.count("DEAD:1") == 2


# --- keep-warm grace ---------------------------------------------------------- #


async def test_resubscribe_within_grace_is_served_from_cache():
    connector = SlowConnector(get_delay_s=0.2)
    broker = PVBroker(connector, _config(monitor_grace_seconds=5.0))
    ws = _TimedSocket()
    broker.add_client("c1", ws)

    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])
    # Still monitored and cached while in grace.
    assert "LIVE:1" in connector.callbacks
    assert "LIVE:1" in broker._latest

    t0 = time.monotonic()
    await broker.subscribe("c1", ["LIVE:1"])
    assert time.monotonic() - t0 < 0.1
    assert connector.gets.count("LIVE:1") == 1
    assert connector.monitors_started.count("LIVE:1") == 1
    assert len(ws.initial_values("LIVE:1")) == 2
    await broker.stop()


async def test_monitor_values_keep_flowing_into_the_cache_during_grace():
    connector = SlowConnector()
    broker = PVBroker(connector, _config(monitor_grace_seconds=5.0))
    ws = _TimedSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])

    connector.emit("LIVE:1", 7.0, ts=50)
    await broker._drain_nowait()
    await broker._flush()
    # Nobody is subscribed, so nothing is sent ...
    assert not [m for m in ws.sent if m["type"] == "diff"]
    # ... but a returning subscriber gets the fresh value.
    await broker.subscribe("c1", ["LIVE:1"])
    assert ws.initial_values("LIVE:1")[-1]["value"] == 7.0
    await broker.stop()


async def test_unsubscribe_after_grace_releases_the_monitor():
    connector = SlowConnector()
    broker = PVBroker(connector, _config(monitor_grace_seconds=0.1))
    broker.add_client("c1", _TimedSocket())

    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])
    await asyncio.sleep(0.25)

    assert "LIVE:1" not in connector.callbacks
    assert "LIVE:1" not in broker._latest
    assert "LIVE:1" not in broker._meta
    assert broker.status()["active_monitors"] == 0


async def test_disconnect_also_gets_the_grace_period():
    connector = SlowConnector()
    broker = PVBroker(connector, _config(monitor_grace_seconds=5.0))
    broker.add_client("c1", _TimedSocket())
    await broker.subscribe("c1", ["LIVE:1"])

    await broker.remove_client("c1")
    assert "LIVE:1" in connector.callbacks

    broker.add_client("c2", _TimedSocket())
    await broker.subscribe("c2", ["LIVE:1"])
    assert connector.gets.count("LIVE:1") == 1
    await broker.stop()


async def test_resubscribe_cancels_the_pending_release():
    connector = SlowConnector()
    broker = PVBroker(connector, _config(monitor_grace_seconds=0.1))
    broker.add_client("c1", _TimedSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])
    await broker.subscribe("c1", ["LIVE:1"])

    await asyncio.sleep(0.25)
    assert "LIVE:1" in connector.callbacks
    assert "LIVE:1" in broker._latest


async def test_stop_closes_monitors_still_in_grace():
    connector = SlowConnector()
    broker = PVBroker(connector, _config(monitor_grace_seconds=60.0))
    broker.add_client("c1", _TimedSocket())
    await broker.subscribe("c1", ["LIVE:1"])
    await broker.unsubscribe("c1", ["LIVE:1"])

    await broker.stop()
    assert "LIVE:1" not in connector.callbacks
    assert not broker._grace


def test_defaults_keep_monitors_warm_and_cache_dead_names():
    cfg = HareConfig()
    assert cfg.monitor_grace_seconds == 60.0
    assert cfg.negative_cache_ttl_s > 0


# --- endpoint ------------------------------------------------------------------ #


@pytest.fixture
def slow_app():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    app = FastAPI()
    app.include_router(ws_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: broker
    app.dependency_overrides[ws_require_read_access] = lambda: None
    return app


def test_ws_read_loop_is_not_blocked_by_a_dead_pv(slow_app):
    with TestClient(slow_app) as client:
        with client.websocket_connect("/ws/pvs") as ws:
            t0 = time.monotonic()
            ws.send_json({"type": "subscribe", "pvNames": ["DEAD:1"]})
            ws.send_json({"type": "subscribe", "pvNames": ["LIVE:1"]})
            first = ws.receive_json()
            elapsed = time.monotonic() - t0
            assert first["type"] == "initial"
            assert "LIVE:1" in first["data"]
            assert elapsed < DEAD_DELAY_S / 2
            second = ws.receive_json()
            assert second["type"] == "initial"
            assert second["data"]["DEAD:1"]["connected"] is False
