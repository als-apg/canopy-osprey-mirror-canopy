"""Hare WebSocket endpoint — end-to-end over the simulation backend."""

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from canopy.main import app


def test_ws_subscribe_streams_values():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "pvNames": ["SIM:WS"]})

            sample = None
            for _ in range(5):
                msg = ws.receive_json()
                if msg["type"] in ("initial", "diff") and "SIM:WS" in msg.get("data", {}):
                    sample = msg["data"]["SIM:WS"]
                    break

            assert sample is not None, "expected an initial/diff frame carrying SIM:WS"
            assert sample["pv"] == "SIM:WS"
            assert sample["connected"] is True
            assert isinstance(sample["value"], int | float)
            assert sample["valueKind"] == "scalar"
            assert "severity" in sample


def test_ws_initial_frame_carries_metadata():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "pvNames": ["SIM:STATE:ENUM"]})

            initial = None
            for _ in range(5):
                msg = ws.receive_json()
                if msg["type"] == "initial":
                    initial = msg
                    break

            assert initial is not None, "expected an initial frame"
            assert "meta" in initial
            meta = initial["meta"]["SIM:STATE:ENUM"]
            assert meta["valueKind"] == "enum"
            assert meta["enumChoices"]
            assert initial["data"]["SIM:STATE:ENUM"]["valueKind"] == "enum"


def test_ws_ping_pong():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
            assert msg["type"] == "pong"


def test_ws_requires_auth():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/hare/v1/ws/pvs") as ws:
                ws.receive_json()
