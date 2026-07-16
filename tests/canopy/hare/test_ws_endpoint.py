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


def test_ws_subscribe_with_invalid_name_does_not_crash_connection():
    # The PVA:// incident, end-to-end: a bare-prefix name in the batch must be
    # dropped without killing the socket, and the valid PV still streams.
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "pvNames": ["pva://", "SIM:WS"]})

            got_valid = False
            for _ in range(5):
                msg = ws.receive_json()
                if msg["type"] in ("initial", "diff") and "SIM:WS" in msg.get("data", {}):
                    got_valid = True
                    break

            assert got_valid, "valid PV should still stream despite the invalid name"

            # Connection is still alive and responsive.
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


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


def test_ws_non_object_message_errors_without_dropping():
    # A JSON array/scalar used to raise AttributeError on message.get and drop
    # the connection. Now it returns an error frame and stays open.
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json([1, 2, 3])
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "invalid_message"
            # still alive
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_ws_non_json_text_message_errors_without_dropping():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_text("this is not json")
            err = ws.receive_json()
            assert err["type"] == "error"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_ws_pvnames_as_string_is_rejected_not_exploded():
    # "MYPV" must not become ['M','Y','P','V'] -> 4 bogus monitors.
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "pvNames": "SIM:WS"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == "invalid_message"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_ws_unknown_type_errors_without_dropping():
    with TestClient(app) as client:
        with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
            ws.send_json({"type": "frobnicate"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert "frobnicate" in err["message"]
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
