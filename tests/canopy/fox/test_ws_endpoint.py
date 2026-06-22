"""Fox WebSocket endpoint — end-to-end tests."""

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.fox.app import app as fox_app
from canopy.fox.config import FoxConfig
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.fox.dependencies import get_formula_service, get_subscription_manager
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.vole.services.notifier import ChangeNotifier
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import SubscriptionManager


@pytest.fixture
def notifier():
    return ChangeNotifier()


@pytest.fixture
def ws_client(notifier):
    """TestClient with Fox's FormulaService and SubscriptionManager overridden."""
    data_source = FakeDataSource(
        latest={
            "PV:A": Sample(ts_ns=1000, value=5.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
            "PV:B": Sample(ts_ns=2000, value=3.0, severity=AlarmSeverity.NO_ALARM, source="archive"),
        }
    )
    config = FoxConfig()
    parser = Parser()
    formula_service = FormulaService(data_source, parser, config)

    subscribed_pvs = []

    async def subscribe_live(pvs: list[str]) -> None:
        subscribed_pvs.extend(pvs)

    sub_manager = SubscriptionManager(
        formula_service=formula_service,
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
    )

    fox_app.dependency_overrides[get_formula_service] = lambda: formula_service
    fox_app.dependency_overrides[get_subscription_manager] = lambda: sub_manager
    try:
        with TestClient(parent_app) as client:
            yield client, notifier, sub_manager
    finally:
        fox_app.dependency_overrides.pop(get_formula_service, None)
        fox_app.dependency_overrides.pop(get_subscription_manager, None)


_SCALAR_SPEC = {
    "kind": "scalar",
    "expression": "{a} + {b}",
    "bindings": {"a": "PV:A", "b": "PV:B"},
}


class TestFoxWebSocket:
    def test_ws_subscribe_receives_result(self, ws_client):
        client, notifier, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "spec": _SCALAR_SPEC})

            msg = ws.receive_json()
            assert msg["type"] == "subscribed"
            sub_id = msg["id"]
            assert sub_id

            msg = ws.receive_json()
            assert msg["type"] == "result"
            assert msg["id"] == sub_id
            assert msg["data"]["kind"] == "scalar"
            assert msg["data"]["scalar"]["value"] == 8.0

    def test_ws_ping_pong(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
            assert msg["type"] == "pong"
            assert "timestamp" in msg

    def test_ws_bad_message_type(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "bogus"})
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "unknown message type" in msg["message"]

    def test_ws_requires_auth(self, ws_client):
        client, _, _ = ws_client
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/fox/v1/ws/formulas") as ws:
                ws.receive_json()

    def test_ws_invalid_spec(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "spec": {"bad": "data"}})
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "invalid spec" in msg["message"]

    def test_ws_subscribe_missing_spec(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe"})
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "requires" in msg["message"]

    def test_ws_one_shot_sends_completed(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json(
                {
                    "type": "subscribe",
                    "spec": {
                        "kind": "timeseries",
                        "expression": "{a} + {b}",
                        "bindings": {"a": "PV:A", "b": "PV:B"},
                    },
                    "t0Ns": 1_000_000_000,
                    "t1Ns": 10_000_000_000,
                }
            )

            msg = ws.receive_json()
            assert msg["type"] == "subscribed"
            sub_id = msg["id"]

            msg = ws.receive_json()
            assert msg["type"] == "result"
            assert msg["id"] == sub_id

            msg = ws.receive_json()
            assert msg["type"] == "completed"
            assert msg["id"] == sub_id

    def test_ws_unsubscribe(self, ws_client):
        client, _, _ = ws_client
        with client.websocket_connect("/fox/v1/ws/formulas?api_key=test-read") as ws:
            ws.send_json({"type": "subscribe", "spec": _SCALAR_SPEC})

            msg = ws.receive_json()
            assert msg["type"] == "subscribed"
            sub_id = msg["id"]

            msg = ws.receive_json()
            assert msg["type"] == "result"

            ws.send_json({"type": "unsubscribe", "id": sub_id})
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
            assert msg["type"] == "pong"
