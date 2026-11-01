"""Heartbeat frame: one broker helper, first on every Hare socket.

The heartbeat carries ``backend`` plus, for a connector implementing
`ConnectorStatement`, its identity (``label``, ``notice``, ``binding``) and its
statement (``reachable``, ``writes``), read on every call so a change shows on
the next frame. A connector without the seam yields ``None`` for all five. The
same frame opens every ``ws_pvs`` socket and is sent by the keep-alive loop.

The flush loop compares (label, notice, binding, reachable, writes) on every
tick, connected clients or not, and sends the frame at once when it differs
from the last one pushed, then calls the connector's ``statement_sent()``. The
keep-alive loop never calls it.
"""

import asyncio
from dataclasses import field, dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.epics.base import ConnectorStatement
from canopy.hare.api.v1 import ws as ws_module
from canopy.hare.config import HareConfig
from canopy.core.security import ws_require_read_access
from canopy.hare.dependencies import get_pv_broker
from tests.canopy.hare.conftest import FakeConnector, FakeWebSocket
from canopy.hare.services.broker import PVBroker

FRAME_KEYS = {"type", "timestamp", "backend", "label", "notice", "binding", "reachable", "writes"}


@dataclass
class StatementConnector(FakeConnector):
    """FakeConnector implementing `ConnectorStatement`, counting ``statement_sent()`` calls."""

    label: str | None = "Test ring"
    notice: str | None = "fallback"
    binding: str | None = "ring@3"
    reachable: dict | None = field(default_factory=lambda: {"state": "yes", "since": None, "reason": None})
    writes: dict | None = field(default_factory=lambda: {"state": "on", "reason": None, "cause": None})
    sent_count: int = 0

    def statement_sent(self) -> None:
        self.sent_count += 1


@dataclass
class LabelOnlyConnector(FakeConnector):
    """Has a ``label`` but not the rest of the seam: no statement at all."""

    label: str | None = "half a seam"


class FakeClock:
    """Injected clock for the broker loops: a sleep returns only when advanced past it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future]] = []

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    async def advance(self, delay: float) -> None:
        """Move time forward, wake every due sleeper, and let the woken loops run."""
        self.now += delay
        due = [f for t, f in self._sleepers if t <= self.now + 1e-9]
        self._sleepers = [(t, f) for t, f in self._sleepers if t > self.now + 1e-9]
        for future in due:
            if not future.done():
                future.set_result(None)
        for _ in range(20):
            await asyncio.sleep(0)


TICK = 0.1  # ws_batch_interval_ms=100
KEEP_ALIVE = 5.0
TICKS_PER_KEEP_ALIVE = round(KEEP_ALIVE / TICK)


def _config(**overrides) -> HareConfig:
    base = {"ws_batch_interval_ms": 10, "heartbeat_interval_s": 100.0}
    base.update(overrides)
    return HareConfig(**base)


async def _clocked_broker(connector, *, channels: int = 1) -> tuple[PVBroker, FakeClock, list[FakeWebSocket]]:
    broker = PVBroker(connector, _config(ws_batch_interval_ms=100, heartbeat_interval_s=KEEP_ALIVE))
    clock = FakeClock()
    broker._sleep = clock.sleep
    sockets = [FakeWebSocket() for _ in range(channels)]
    for i, socket in enumerate(sockets):
        broker.add_client(f"c{i}", socket)
    await broker.start()
    await clock.advance(0)
    return broker, clock, sockets


def _beats(socket: FakeWebSocket) -> list[dict]:
    return [m for m in socket.sent if m.get("type") == "heartbeat"]


def _app(broker: PVBroker) -> FastAPI:
    app = FastAPI()
    app.include_router(ws_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: broker
    app.dependency_overrides[ws_require_read_access] = lambda: None
    return app


# --- the helper --------------------------------------------------------------- #


def test_the_test_connector_implements_the_seam():
    assert isinstance(StatementConnector(), ConnectorStatement)
    assert not isinstance(LabelOnlyConnector(), ConnectorStatement)


def test_frame_carries_connector_identity_and_statement():
    frame = PVBroker(StatementConnector(), _config()).heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    assert frame["type"] == "heartbeat"
    assert frame["backend"] == "fake"
    assert (frame["label"], frame["notice"], frame["binding"]) == ("Test ring", "fallback", "ring@3")
    assert frame["reachable"] == {"state": "yes", "since": None, "reason": None}
    assert frame["writes"] == {"state": "on", "reason": None, "cause": None}
    assert isinstance(frame["timestamp"], float) and frame["timestamp"] > 0


def test_frame_is_null_for_a_connector_without_the_seam():
    for connector in (FakeConnector(), LabelOnlyConnector()):
        frame = PVBroker(connector, _config()).heartbeat_frame()
        assert set(frame) == FRAME_KEYS
        assert frame["backend"] == "fake"
        assert [frame[k] for k in ("label", "notice", "binding", "reachable", "writes")] == [None] * 5


def test_frame_reads_the_statement_on_every_call():
    connector = StatementConnector()
    broker = PVBroker(connector, _config())
    assert broker.heartbeat_frame()["binding"] == "ring@3"
    connector.label, connector.notice, connector.binding = "Linac", None, "linac@4"
    frame = broker.heartbeat_frame()
    assert (frame["label"], frame["notice"], frame["binding"]) == ("Linac", None, "linac@4")
    connector.label = connector.notice = connector.binding = connector.reachable = connector.writes = None
    frame = broker.heartbeat_frame()
    assert [frame[k] for k in ("label", "notice", "binding", "reachable", "writes")] == [None] * 5


def test_add_client_alone_sends_nothing():
    # The accept-time heartbeat belongs to ws_pvs, not add_client: in-process
    # clients (Vole) register through add_client and must not get it.
    broker = PVBroker(StatementConnector(), _config())
    channel = FakeWebSocket()
    broker.add_client("in-process", channel)
    assert channel.sent == []


# --- the keep-alive loop ------------------------------------------------------ #


async def test_keep_alive_sends_the_helper_frame():
    broker, clock, (socket,) = await _clocked_broker(StatementConnector())
    try:
        for _ in range(TICKS_PER_KEEP_ALIVE):
            await clock.advance(TICK)
    finally:
        await broker.stop()
    (beat,) = _beats(socket)
    assert set(beat) == FRAME_KEYS
    assert (beat["label"], beat["notice"], beat["binding"]) == ("Test ring", "fallback", "ring@3")


# --- statement push on the flush tick (injected clock) ------------------------ #


async def test_statement_change_is_sent_within_one_flush_tick():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        await clock.advance(TICK)
        assert _beats(socket) == []
        connector.writes = {"state": "applying", "reason": "switching target", "cause": None}
        await clock.advance(TICK)
        beats = _beats(socket)
        assert len(beats) == 1
        assert set(beats[0]) == FRAME_KEYS
        assert beats[0]["writes"] == {"state": "applying", "reason": "switching target", "cause": None}
        assert connector.sent_count == 1
        for attr, value in (
            ("reachable", {"state": "no", "since": 1760700000.0, "reason": "gateway down"}),
            ("label", "Linac"),
            ("notice", None),
            ("binding", "linac@4"),
        ):
            setattr(connector, attr, value)
            await clock.advance(TICK)
            assert _beats(socket)[-1][attr] == value
        assert len(_beats(socket)) == 5
        assert connector.sent_count == 5
    finally:
        await broker.stop()


async def test_in_place_statement_update_is_a_change():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        connector.writes["state"] = "off"
        await clock.advance(TICK)
        assert [b["writes"]["state"] for b in _beats(socket)] == ["off"]
        assert connector.sent_count == 1
    finally:
        await broker.stop()


async def test_no_change_sends_only_the_keep_alive():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        for _ in range(TICKS_PER_KEEP_ALIVE - 1):
            await clock.advance(TICK)
        assert _beats(socket) == []
        await clock.advance(TICK)
        assert len(_beats(socket)) == 1
        for _ in range(TICKS_PER_KEEP_ALIVE):
            await clock.advance(TICK)
        assert len(_beats(socket)) == 2
        assert connector.sent_count == 0
    finally:
        await broker.stop()


async def test_statement_sent_once_per_change_never_after_a_keep_alive():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        connector.reachable = {"state": "unknown", "since": None, "reason": None}
        await clock.advance(TICK)
        assert connector.sent_count == 1
        # Unchanged ticks up to and past a keep-alive: a frame goes out, no call.
        for _ in range(TICKS_PER_KEEP_ALIVE):
            await clock.advance(TICK)
        assert len(_beats(socket)) == 2
        assert connector.sent_count == 1
        # A change and back again: two changes, two calls.
        connector.reachable = {"state": "yes", "since": None, "reason": None}
        await clock.advance(TICK)
        connector.reachable = {"state": "unknown", "since": None, "reason": None}
        await clock.advance(TICK)
        assert connector.sent_count == 3
        assert len(_beats(socket)) == 4
    finally:
        await broker.stop()


async def test_statement_change_with_no_client_still_compares_and_calls_once():
    connector = StatementConnector()
    broker, clock, _ = await _clocked_broker(connector, channels=0)
    try:
        await clock.advance(TICK)
        assert connector.sent_count == 0
        connector.writes = {"state": "locked", "reason": "locked by an operator", "cause": None}
        await clock.advance(TICK)
        assert connector.sent_count == 1
        for _ in range(TICKS_PER_KEEP_ALIVE + 1):
            await clock.advance(TICK)
        assert connector.sent_count == 1
    finally:
        await broker.stop()


async def test_a_raising_statement_sent_keeps_the_flush_loop_alive():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)

    def boom() -> None:
        connector.sent_count += 1
        raise RuntimeError("connector bug")

    connector.statement_sent = boom
    try:
        connector.writes = {"state": "off", "reason": None, "cause": None}
        await clock.advance(TICK)
        connector.writes = {"state": "on", "reason": None, "cause": None}
        await clock.advance(TICK)
        assert connector.sent_count == 2
        assert len(_beats(socket)) == 2
        assert broker._tasks and not any(t.done() for t in broker._tasks)
    finally:
        await broker.stop()


# --- ws_pvs: the heartbeat is the first frame ---------------------------------- #


def test_ws_first_frame_is_the_heartbeat():
    broker = PVBroker(StatementConnector(), _config())
    with TestClient(_app(broker)) as client:
        with client.websocket_connect("/ws/pvs") as ws:
            first = ws.receive_json()
            assert set(first) == FRAME_KEYS
            assert first["type"] == "heartbeat"
            assert first["backend"] == "fake"
            assert (first["label"], first["notice"], first["binding"]) == ("Test ring", "fallback", "ring@3")
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_ws_first_frame_precedes_subscribe_output():
    broker = PVBroker(FakeConnector(), _config())
    with TestClient(_app(broker)) as client:
        with client.websocket_connect("/ws/pvs") as ws:
            first = ws.receive_json()
            assert first["type"] == "heartbeat"
            assert (first["label"], first["notice"], first["binding"]) == (None, None, None)
            ws.send_json({"type": "subscribe", "pvNames": ["LIVE:1"]})
            assert ws.receive_json()["type"] == "initial"
