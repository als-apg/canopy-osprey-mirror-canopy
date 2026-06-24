"""PVBroker: on-demand monitors, diff fan-out, refcounted teardown.

Driven deterministically via FakeConnector (emit on demand) + FakeWebSocket,
without starting the background loops — tests call `_drain_nowait` / `_flush`.
"""

import pytest

from canopy.epics.base import PVWriteFailed
from canopy.contracts.sample import AlarmSeverity
from tests.canopy.hare.conftest import FakeWebSocket


async def test_subscribe_starts_monitor_and_sends_initial(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])

    assert "PV:A" in fake_connector.callbacks  # monitor started
    assert ws.sent and ws.sent[0]["type"] == "initial"
    assert "PV:A" in ws.sent[0]["data"]


async def test_event_flushed_as_diff(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    ws.sent.clear()

    fake_connector.emit("PV:A", 1.23, severity=AlarmSeverity.MINOR, ts=99)
    await broker._drain_nowait()
    await broker._flush()

    diffs = [m for m in ws.sent if m["type"] == "diff"]
    assert diffs
    payload = diffs[-1]["data"]["PV:A"]
    assert payload["value"] == 1.23
    assert payload["severity"] == "MINOR"
    assert payload["tsNs"] == 99  # camelCase on the wire


async def test_diff_only_goes_to_subscribers(broker, fake_connector):
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)
    await broker.subscribe("c1", ["PV:A"])
    await broker.subscribe("c2", ["PV:B"])
    ws1.sent.clear()
    ws2.sent.clear()

    fake_connector.emit("PV:A", 5.0)
    await broker._drain_nowait()
    await broker._flush()

    assert any(m["type"] == "diff" for m in ws1.sent)
    assert not any(m["type"] == "diff" for m in ws2.sent)


async def test_refcount_keeps_monitor_until_last_subscriber_leaves(broker, fake_connector):
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    broker.add_client("c1", ws1)
    broker.add_client("c2", ws2)
    await broker.subscribe("c1", ["PV:A"])
    await broker.subscribe("c2", ["PV:A"])

    await broker.unsubscribe("c1", ["PV:A"])
    assert "PV:A" in fake_connector.callbacks  # c2 still subscribed

    await broker.unsubscribe("c2", ["PV:A"])
    assert "PV:A" not in fake_connector.callbacks  # monitor closed


async def test_remove_client_closes_its_monitors(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    await broker.remove_client("c1")
    assert "PV:A" not in fake_connector.callbacks


async def test_get_value_falls_back_to_one_shot(broker, fake_connector):
    # Not subscribed -> broker issues a one-shot connector.get
    value = await broker.get_value("PV:Z")
    assert value.pv == "PV:Z"
    assert value.connected is True


async def test_initial_frame_includes_metadata(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])

    initial = ws.sent[0]
    assert initial["type"] == "initial"
    assert "meta" in initial
    meta = initial["meta"]["PV:A"]
    assert meta["valueKind"] == "scalar"  # camelCase on the wire
    assert meta["units"] == "mm"


async def test_diff_frame_carries_no_metadata(broker, fake_connector):
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:A"])
    ws.sent.clear()

    fake_connector.emit("PV:A", 1.0)
    await broker._drain_nowait()
    await broker._flush()

    diff = [m for m in ws.sent if m["type"] == "diff"][-1]
    assert "meta" not in diff
    assert diff["data"]["PV:A"]["valueKind"] == "scalar"


async def test_get_describe_falls_back_to_one_shot(broker):
    meta = await broker.get_describe("PV:Q")
    assert meta.pv == "PV:Q"
    assert meta.value_kind.value == "scalar"


async def test_put_delegates_to_connector(broker, fake_connector):
    await broker.put("PV:A", 42.0)
    assert fake_connector.puts == [("PV:A", 42.0, True, None)]


async def test_put_passes_wait_and_timeout(broker, fake_connector):
    await broker.put("PV:A", 1, wait=False, timeout=0.5)
    assert fake_connector.puts == [("PV:A", 1, False, 0.5)]


async def test_put_propagates_connector_failure(broker, fake_connector):
    fake_connector.put_error = PVWriteFailed("boom")
    with pytest.raises(PVWriteFailed):
        await broker.put("PV:A", 1.0)


async def test_put_invalidates_cache_so_read_is_fresh(broker, fake_connector):
    # A monitored PV's value is cached; after a put, get_value must not serve the
    # stale cache -- it re-reads through the connector so put-then-read is consistent.
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:A"])
    fake_connector.emit("PV:A", 1.0)
    await broker._drain_nowait()
    assert (await broker.get_value("PV:A")).value == 1.0  # served from cache

    await broker.put("PV:A", 9.9)
    assert "PV:A" not in broker._latest  # cache invalidated
    assert (await broker.get_value("PV:A")).value == 0.0  # fresh one-shot connector.get
