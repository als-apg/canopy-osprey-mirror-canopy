"""Broker: the flush marker for Vole, per-flush coalescing, and the late-connect re-describe.

- Every ``_flush`` -- empty ones included -- sends ``{type: 'flush', timestamp}`` to
  the channels that ask for it (``wants_flush_marker``); browser sockets never see it
  (b6:IA-13).
- ``_ingest`` keeps one value per PV per flush, so Vole rings hold at most one
  sample per PV per flush (b6:IA-5).
- A PV unreachable at add time is re-described on its first successful connect,
  the meta is stored, and every holder gets an ``initial`` carrying it (b6:IA-1,
  b6:M-1). The describe never runs on the consume loop (b7:IA-1).
"""

from __future__ import annotations

import time
import asyncio

from canopy.hare.services.broker import PVBroker
from tests.canopy.hare.conftest import FakeWebSocket
from tests.canopy.hare.test_broker_seeding import DEAD_DELAY_S, SlowConnector, _config, _wait_for


class VoleLikeSocket(FakeWebSocket):
    wants_flush_marker = True


def _frames(ws: FakeWebSocket, kind: str) -> list[dict]:
    return [m for m in ws.sent if m["type"] == kind]


# --- flush marker ------------------------------------------------------------- #


async def test_flush_marker_goes_to_marker_clients_on_every_flush_including_empty(broker, fake_connector):
    vole, browser = VoleLikeSocket(), FakeWebSocket()
    broker.add_client("vole", vole)
    broker.add_client("browser", browser)
    await broker.subscribe("vole", ["P"])
    await broker.subscribe("browser", ["P"])

    await broker._flush()  # nothing buffered
    assert len(_frames(vole, "flush")) == 1
    assert _frames(browser, "flush") == []

    fake_connector.emit("P", 1.0, ts=10)
    await broker._drain_nowait()
    await broker._flush()
    assert [m["type"] for m in vole.sent[-2:]] == ["diff", "flush"]  # marker after the diff it covers
    assert _frames(browser, "flush") == []
    assert all(isinstance(m["timestamp"], float) for m in _frames(vole, "flush"))


async def test_flush_marker_reaches_a_marker_client_with_no_subscriptions(broker):
    vole = VoleLikeSocket()
    broker.add_client("vole", vole)
    await broker._flush()
    assert len(_frames(vole, "flush")) == 1


# --- coalescing --------------------------------------------------------------- #


async def test_a_fast_pv_keeps_one_sample_per_flush(broker, fake_connector):
    vole = VoleLikeSocket()
    broker.add_client("vole", vole)
    await broker.subscribe("vole", ["FAST"])
    for i in range(10):
        fake_connector.emit("FAST", float(i), ts=100 + i)
    await broker._drain_nowait()
    await broker._flush()
    diffs = _frames(vole, "diff")
    assert len(diffs) == 1 and diffs[0]["count"] == 1
    assert diffs[0]["data"]["FAST"]["value"] == 9.0


# --- late-connect re-describe ------------------------------------------------- #


async def test_first_connect_of_an_unreachable_pv_redescribes_and_sends_initial_with_meta():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)
    await broker.subscribe("c1", ["DEAD:1"])
    await broker.subscribe("c2", ["DEAD:1"])
    first = _frames(ws1, "initial")[0]
    assert first["data"]["DEAD:1"]["connected"] is False
    assert first["meta"]["DEAD:1"]["units"] is None

    connector.dead.discard("DEAD:1")  # the IOC comes up
    connector.emit("DEAD:1", 5.0, ts=50)
    await broker._drain_nowait()
    await _wait_for(lambda: connector.describes.count("DEAD:1") == 2)
    await _wait_for(lambda: any(m["meta"].get("DEAD:1", {}).get("units") == "mm" for m in _frames(ws1, "initial")))

    assert broker._meta["DEAD:1"].units == "mm"
    for ws in (ws1, ws2):
        last = _frames(ws, "initial")[-1]
        assert last["meta"]["DEAD:1"]["units"] == "mm"
        assert last["data"]["DEAD:1"]["value"] == 5.0 and last["data"]["DEAD:1"]["connected"] is True
    await broker.stop()


async def test_redescribe_runs_once_per_connect_and_not_when_meta_is_known():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["DEAD:1", "LIVE:1"])
    assert connector.describes.count("LIVE:1") == 1

    connector.dead.discard("DEAD:1")
    for i in range(5):
        connector.emit("DEAD:1", float(i), ts=50 + i)
        connector.emit("LIVE:1", float(i), ts=50 + i)
    await broker._drain_nowait()
    await _wait_for(lambda: connector.describes.count("DEAD:1") == 2)
    await asyncio.sleep(0.05)
    assert connector.describes.count("DEAD:1") == 2  # deduped
    assert connector.describes.count("LIVE:1") == 1  # known meta: never re-described
    await broker.stop()


async def test_a_hung_redescribe_delays_neither_another_pvs_diff_nor_the_flush_marker():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    vole = VoleLikeSocket()
    broker.add_client("vole", vole)
    await broker.subscribe("vole", ["DEAD:1", "LIVE:1"])

    connector.dead.discard("DEAD:1")
    connector.describe_delay_s = DEAD_DELAY_S  # the re-describe now hangs
    connector.emit("DEAD:1", 1.0, ts=50)
    connector.emit("LIVE:1", 2.0, ts=51)
    t0 = time.monotonic()
    await broker._drain_nowait()
    await broker._flush()
    elapsed = time.monotonic() - t0

    assert elapsed < DEAD_DELAY_S / 5
    diff = _frames(vole, "diff")[-1]
    assert diff["data"]["LIVE:1"]["value"] == 2.0 and diff["data"]["DEAD:1"]["value"] == 1.0
    assert vole.sent[-1]["type"] == "flush"
    await _wait_for(lambda: broker._meta["DEAD:1"].units == "mm", timeout=DEAD_DELAY_S * 4)
    await broker.stop()


async def test_redescribe_is_dropped_when_the_pv_was_released_meanwhile():
    connector = SlowConnector(dead={"DEAD:1"})
    broker = PVBroker(connector, _config())
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["DEAD:1"])
    connector.dead.discard("DEAD:1")
    connector.describe_delay_s = 0.1
    connector.emit("DEAD:1", 1.0, ts=50)
    await broker._drain_nowait()
    await broker.unsubscribe("c1", ["DEAD:1"])
    await asyncio.sleep(0.3)
    assert "DEAD:1" not in broker._meta
    assert not any(m["meta"].get("DEAD:1", {}).get("units") for m in _frames(ws, "initial"))
    await broker.stop()
