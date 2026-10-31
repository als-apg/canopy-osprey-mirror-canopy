"""Broker: the ``flush`` marker for in-process channels that ask for it.

Every ``_flush`` -- empty ones included -- sends ``{type: 'flush', timestamp}`` to
the channels that set ``wants_flush_marker``, after the diff it covers. Browser
sockets never see it. The buffer keeps one value per PV per flush, so such a
channel receives at most one sample per PV per marker.
"""

from tests.canopy.hare.conftest import FakeWebSocket, settle


class MarkerSocket(FakeWebSocket):
    wants_flush_marker = True


def _frames(ws: FakeWebSocket, kind: str) -> list[dict]:
    return [m for m in ws.sent if m["type"] == kind]


async def test_flush_marker_goes_to_marker_clients_on_every_flush_including_empty(broker, fake_connector):
    marked, browser = MarkerSocket(), FakeWebSocket()
    broker.add_client("in-process", marked)
    broker.add_client("browser", browser)
    await broker.subscribe("in-process", ["P"])
    await broker.subscribe("browser", ["P"])
    await settle(broker)
    before = len(_frames(marked, "flush"))

    await broker._flush()  # nothing buffered
    assert len(_frames(marked, "flush")) == before + 1
    assert _frames(browser, "flush") == []

    fake_connector.emit("P", 1.0, ts=10)
    await broker._drain_nowait()
    await broker._flush()
    assert [m["type"] for m in marked.sent[-2:]] == ["diff", "flush"]  # marker after the diff it covers
    assert _frames(browser, "flush") == []
    assert all(isinstance(m["timestamp"], float) for m in _frames(marked, "flush"))


async def test_flush_marker_reaches_a_marker_client_with_no_subscriptions(broker):
    marked = MarkerSocket()
    broker.add_client("in-process", marked)
    await broker._flush()
    assert len(_frames(marked, "flush")) == 1


async def test_a_fast_pv_keeps_one_sample_per_flush(broker, fake_connector):
    marked = MarkerSocket()
    broker.add_client("in-process", marked)
    await broker.subscribe("in-process", ["FAST"])
    await settle(broker)
    for i in range(10):
        fake_connector.emit("FAST", float(i), ts=100 + i)
    await broker._drain_nowait()
    marked.sent.clear()
    await broker._flush()
    (diff,) = _frames(marked, "diff")
    assert diff["count"] == 1
    assert diff["data"]["FAST"]["value"] == 9.0
