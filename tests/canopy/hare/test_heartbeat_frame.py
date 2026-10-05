"""Heartbeat frame: one broker helper, first on every Hare socket.

The heartbeat carries ``backend`` plus the connector's identity -- ``label``,
``notice`` and ``binding`` (from the connector's ``binding_id``) -- read by
``getattr`` on every call, so a connector without the attributes yields
``None`` and one that changes target is reported on the next frame. The same
frame opens every ``ws_pvs`` socket, is sent by the heartbeat loop, and backs
the key-guarded ``GET /hare/v1/health/summary``.
"""

import asyncio
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.hare.api.v1 import ws as ws_module
from canopy.hare.api.v1 import health as health_module
from canopy.hare.config import HareConfig
from canopy.core.security import require_read_access, ws_require_read_access
from canopy.hare.dependencies import get_pv_broker
from tests.canopy.hare.conftest import FakeConnector, FakeWebSocket
from canopy.hare.services.broker import PVBroker

FRAME_KEYS = {"type", "timestamp", "backend", "label", "notice", "binding"}


@dataclass
class IdentityConnector(FakeConnector):
    """FakeConnector exposing exactly the three identity attributes."""

    label: str | None = "ALS storage ring"
    notice: str | None = "fallback"
    binding_id: str | None = "als-sr@3"


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
