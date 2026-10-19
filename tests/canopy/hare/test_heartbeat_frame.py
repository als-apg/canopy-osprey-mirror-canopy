"""Heartbeat frame: one broker helper, first on every Hare socket.

The heartbeat carries ``backend`` plus the connector's identity -- ``label``,
``notice`` and ``binding`` (from the connector's ``binding_id``) -- read by
``getattr`` on every call, so a connector without the attributes yields
``None`` and one that changes target is reported on the next frame. The same
frame opens every ``ws_pvs`` socket, is sent by the heartbeat loop, and backs
the key-guarded ``GET /hare/v1/health/summary``.

The frame also carries the connector's statement -- ``reachable`` and ``writes``,
read the same way; absent or null means no claim. The flush loop compares
(label, notice, binding, reachable, writes) on every tick, connected channels or
not, and sends the frame at once when it differs from the last one pushed, then
calls the connector's optional ``statement_sent()``. The heartbeat loop stays the
keep-alive and never calls it. Every frame case of ``heartbeat_vectors.json``,
the legacy frame included, is what ``heartbeat_frame()`` builds.
"""

import asyncio
from dataclasses import field, dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.contracts import load_heartbeat_vectors
from canopy.hare.api.v1 import ws as ws_module
from canopy.hare.api.v1 import health as health_module
from canopy.hare.config import HareConfig
from canopy.core.security import require_read_access, ws_require_read_access
from canopy.hare.dependencies import get_pv_broker
from tests.canopy.hare.conftest import FakeConnector, FakeWebSocket
from canopy.hare.services.broker import PVBroker

FRAME_KEYS = {"type", "timestamp", "backend", "label", "notice", "binding", "reachable", "writes"}


@dataclass
class IdentityConnector(FakeConnector):
    """FakeConnector exposing exactly the three identity attributes."""

    label: str | None = "ALS storage ring"
    notice: str | None = "fallback"
    binding_id: str | None = "als-sr@3"


@dataclass
class SilentStatementConnector(IdentityConnector):
    """States reachability and writes but has no ``statement_sent()``."""

    reachable: dict | None = None
    writes: dict | None = None


@dataclass
class StatementConnector(SilentStatementConnector):
    """States reachability and writes, and counts ``statement_sent()`` calls."""

    reachable: dict | None = field(default_factory=lambda: {"state": "yes", "since": None, "reason": None})
    writes: dict | None = field(default_factory=lambda: {"state": "on", "reason": None, "cause": None})
    sent_count: int = 0

    def statement_sent(self) -> None:
        self.sent_count += 1


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


def _config(**overrides) -> HareConfig:
    base = {"ws_batch_interval_ms": 10, "heartbeat_interval_s": 100.0, "monitor_grace_seconds": 0.0}
    base.update(overrides)
    return HareConfig(**base)


def _app(broker: PVBroker) -> FastAPI:
    app = FastAPI()
    app.include_router(ws_module.router)
    app.include_router(health_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: broker
    app.dependency_overrides[ws_require_read_access] = lambda: None
    app.dependency_overrides[require_read_access] = lambda: None
    return app


# --- the helper --------------------------------------------------------------- #


def test_frame_carries_connector_identity():
    broker = PVBroker(IdentityConnector(), _config())
    frame = broker.heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    assert frame["type"] == "heartbeat"
    assert frame["backend"] == "fake"
    assert frame["label"] == "ALS storage ring"
    assert frame["notice"] == "fallback"
    assert frame["binding"] == "als-sr@3"
    assert isinstance(frame["timestamp"], float) and frame["timestamp"] > 0


def test_frame_is_null_for_a_connector_without_identity():
    broker = PVBroker(FakeConnector(), _config())
    frame = broker.heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    assert frame["backend"] == "fake"
    assert frame["label"] is None
    assert frame["notice"] is None
    assert frame["binding"] is None


def test_frame_reads_identity_on_every_call():
    connector = IdentityConnector()
    broker = PVBroker(connector, _config())
    assert broker.heartbeat_frame()["binding"] == "als-sr@3"
    connector.label, connector.notice, connector.binding_id = "BTS", None, "bts@4"
    frame = broker.heartbeat_frame()
    assert (frame["label"], frame["notice"], frame["binding"]) == ("BTS", None, "bts@4")
    # Before start() / after close() an adapter reports all three as None.
    connector.label = connector.notice = connector.binding_id = None
    frame = broker.heartbeat_frame()
    assert (frame["label"], frame["notice"], frame["binding"]) == (None, None, None)


def test_frame_carries_the_connector_statement():
    frame = PVBroker(StatementConnector(), _config()).heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    assert frame["reachable"] == {"state": "yes", "since": None, "reason": None}
    assert frame["writes"] == {"state": "on", "reason": None, "cause": None}


def test_frame_statement_is_null_for_a_connector_without_one():
    frame = PVBroker(IdentityConnector(), _config()).heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    assert (frame["reachable"], frame["writes"]) == (None, None)


def test_add_client_alone_sends_nothing():
    # The accept-time heartbeat belongs to ws_pvs, not add_client: in-process
    # channels (Vole) register through add_client and must not get it.
    broker = PVBroker(IdentityConnector(), _config())
    channel = FakeWebSocket()
    broker.add_client("in-process", channel)
    assert channel.sent == []


# --- the heartbeat loop ------------------------------------------------------- #


async def test_heartbeat_loop_sends_the_helper_frame():
    broker = PVBroker(IdentityConnector(), _config(heartbeat_interval_s=0.01))
    channel = FakeWebSocket()
    broker.add_client("c1", channel)
    await broker.start()
    try:
        for _ in range(200):
            if any(m.get("type") == "heartbeat" for m in channel.sent):
                break
            await asyncio.sleep(0.01)
    finally:
        await broker.stop()
    beats = [m for m in channel.sent if m.get("type") == "heartbeat"]
    assert beats, "heartbeat loop sent nothing"
    assert set(beats[0]) == FRAME_KEYS
    assert (beats[0]["label"], beats[0]["notice"], beats[0]["binding"]) == ("ALS storage ring", "fallback", "als-sr@3")


# --- the heartbeat vectors ---------------------------------------------------- #

_VECTORS = load_heartbeat_vectors()
_FRAME_CASES = [_VECTORS["legacy_frame"], *_VECTORS["reachable_frames"], *_VECTORS["writes_frames"]]


@pytest.mark.parametrize("case", _FRAME_CASES, ids=[c["case"] for c in _FRAME_CASES])
def test_frame_matches_the_vector(case):
    vector = case["frame"]
    identity = {
        "backend_name": vector["backend"],
        "label": vector["label"],
        "notice": vector["notice"],
        "binding_id": vector["binding"],
    }
    if case["case"] == "legacy":
        # A legacy connector has no statement attributes at all: the frame
        # carries both keys null, which reads as the legacy frame (no claim).
        assert "reachable" not in vector and "writes" not in vector
        connector = IdentityConnector(**identity)
    else:
        connector = SilentStatementConnector(**identity, reachable=vector["reachable"], writes=vector["writes"])
    frame = PVBroker(connector, _config()).heartbeat_frame()
    assert set(frame) == FRAME_KEYS
    for key in FRAME_KEYS - {"timestamp"}:
        assert frame[key] == vector.get(key), key
    assert (frame["reachable"], frame["writes"]) == (case["expected"]["reachable"], case["expected"]["writes"])


# --- statement push on the flush tick (injected clock) ------------------------ #


async def test_statement_change_is_sent_within_one_flush_tick():
    connector = StatementConnector()
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        await clock.advance(TICK)
        assert _beats(socket) == []
        connector.writes = {"state": "applying", "reason": "switching to sim", "cause": None}
        await clock.advance(TICK)
        beats = _beats(socket)
        assert len(beats) == 1
        assert set(beats[0]) == FRAME_KEYS
        assert beats[0]["writes"] == {"state": "applying", "reason": "switching to sim", "cause": None}
        assert connector.sent_count == 1
        for attr, value in (
            ("reachable", {"state": "no", "since": 1760700000.0, "reason": "gateway down"}),
            ("label", "BTS"),
            ("notice", None),
            ("binding_id", "bts@4"),
        ):
            setattr(connector, attr, value)
            await clock.advance(TICK)
            assert _beats(socket)[-1][attr if attr != "binding_id" else "binding"] == value
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


async def test_connector_without_statement_sent_changes_nothing():
    connector = SilentStatementConnector(writes={"state": "on", "reason": None, "cause": None})
    broker, clock, (socket,) = await _clocked_broker(connector)
    try:
        connector.writes = {"state": "off", "reason": "read-only run", "cause": None}
        await clock.advance(TICK)
        await clock.advance(TICK)
        beats = _beats(socket)
        assert len(beats) == 1
        assert beats[0]["writes"]["state"] == "off"
        assert broker._tasks and not any(t.done() for t in broker._tasks)
    finally:
        await broker.stop()


async def test_statement_change_with_no_channel_still_compares_and_calls_once():
    connector = StatementConnector()
    broker, clock, _ = await _clocked_broker(connector, channels=0)
    try:
        await clock.advance(TICK)
        assert connector.sent_count == 0
        connector.writes = {"state": "locked", "reason": "persona ceiling", "cause": None}
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
        raise RuntimeError("adapter bug")

    connector.statement_sent = boom
    try:
        connector.writes = {"state": "off", "reason": None, "cause": None}
        await clock.advance(TICK)
        connector.writes = {"state": "on", "reason": None, "cause": None}
        await clock.advance(TICK)
        assert connector.sent_count == 2
        assert len(_beats(socket)) == 2
    finally:
        await broker.stop()


# --- ws_pvs: the heartbeat is the first frame ---------------------------------- #


def test_ws_first_frame_is_the_heartbeat():
    broker = PVBroker(IdentityConnector(), _config())
    with TestClient(_app(broker)) as client:
        with client.websocket_connect("/ws/pvs") as ws:
            first = ws.receive_json()
            assert set(first) == FRAME_KEYS
            assert first["type"] == "heartbeat"
            assert first["backend"] == "fake"
            assert first["label"] == "ALS storage ring"
            assert first["notice"] == "fallback"
            assert first["binding"] == "als-sr@3"
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


# --- health summary ----------------------------------------------------------- #


@pytest.mark.parametrize(
    ("connector", "expected"),
    [
        (IdentityConnector(), ("ALS storage ring", "fallback", "als-sr@3")),
        (
            IdentityConnector(label="BTS", notice="unservable", binding_id="unservable"),
            ("BTS", "unservable", "unservable"),
        ),
        (FakeConnector(), (None, None, None)),
    ],
)
def test_summary_carries_the_frame_identity(connector, expected):
    broker = PVBroker(connector, _config())
    with TestClient(_app(broker)) as client:
        body = client.get("/health/summary").json()
    assert (body["label"], body["notice"], body["binding"]) == expected
    frame = broker.heartbeat_frame()
    assert body["backend"] == frame["backend"] == "fake"
    assert (body["label"], body["notice"], body["binding"]) == (frame["label"], frame["notice"], frame["binding"])
