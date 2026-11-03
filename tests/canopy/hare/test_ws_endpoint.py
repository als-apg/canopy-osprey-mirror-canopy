"""Hare WebSocket endpoint — end-to-end over the simulation backend."""

import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from canopy.hare import gateway
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


class _SilentSubscription:
    """Monitor of a channel that never connects: it never emits and closes cleanly."""

    async def close(self) -> None:
        pass


class _NeverConnects:
    """Wrap the live connector so ``DEAD:*`` PVs behave like CA channels that never connect.

    Their monitor never emits, so only the broker's connect timer ever reports
    them; every other PV goes to the wrapped connector.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.backend_name = getattr(inner, "backend_name", None)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def subscribe(self, pv, on_event):
        if pv.startswith("DEAD:"):
            return _SilentSubscription()
        return await self._inner.subscribe(pv, on_event)


def _read_until(ws, predicate, *, budget_s: float) -> tuple[list[dict], float]:
    """Read frames until ``predicate`` matches one; return the frames read and the seconds it took."""
    frames = []
    start = time.monotonic()
    while True:
        msg = ws.receive_json()
        frames.append(msg)
        elapsed = time.monotonic() - start
        if predicate(msg):
            return frames, elapsed
        assert elapsed < budget_s, f"no matching frame within {budget_s}s: {frames}"


def test_ws_dead_channel_does_not_stall_the_socket():
    """A PV whose channel never connects holds up neither a ping nor a later subscribe on the same socket."""
    with TestClient(app) as client:
        broker = gateway.get_broker()
        real = broker._connector
        broker._connector = _NeverConnects(real)
        try:
            with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
                ws.send_json({"type": "subscribe", "pvNames": ["DEAD:NEVER"]})
                ws.send_json({"type": "subscribe", "pvNames": ["SIM:WS"]})
                ws.send_json({"type": "ping"})

                frames, to_pong = _read_until(ws, lambda m: m["type"] == "pong", budget_s=10.0)
                assert to_pong < 2.0, f"pong took {to_pong:.1f}s behind the dead channel's seed"

                def live_initial(m):
                    return m["type"] == "initial" and "SIM:WS" in m.get("meta", {})

                if not any(live_initial(m) for m in frames):
                    more, to_initial = _read_until(ws, live_initial, budget_s=10.0)
                    frames += more
                    assert to_initial < 2.0, f"SIM:WS initial took {to_initial:.1f}s behind the dead channel's seed"
                assert not any("DEAD:NEVER" in m.get("data", {}) for m in frames)
        finally:
            broker._connector = real


def test_ws_dead_channel_in_the_same_frame_does_not_delay_live_metadata():
    """A live PV subscribed in the same frame as a dead one gets its ``initial`` with metadata promptly."""
    with TestClient(app) as client:
        broker = gateway.get_broker()
        real = broker._connector
        broker._connector = _NeverConnects(real)
        try:
            with client.websocket_connect("/hare/v1/ws/pvs?api_key=test-read") as ws:
                ws.send_json({"type": "subscribe", "pvNames": ["DEAD:NEVER", "SIM:STATE:ENUM"]})

                frames, elapsed = _read_until(
                    ws,
                    lambda m: m["type"] == "initial" and "SIM:STATE:ENUM" in m.get("meta", {}),
                    budget_s=10.0,
                )
                assert elapsed < 2.0, f"initial took {elapsed:.1f}s behind the dead channel's seed"
                assert frames[-1]["meta"]["SIM:STATE:ENUM"]["enumChoices"]
                assert "DEAD:NEVER" not in frames[-1]["data"]
        finally:
            broker._connector = real
