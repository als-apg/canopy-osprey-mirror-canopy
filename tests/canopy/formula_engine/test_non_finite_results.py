"""What a scalar result that is NaN or infinite looks like on the wire.

NaN and infinity are not JSON. ``JSON.parse`` refuses a frame that holds a bare
``NaN``, so a browser client that gets one cannot read the result at all.

Two paths serialize a scalar result. The REST endpoint goes through the
response model, which writes ``null``. The WebSocket goes through
``send_json``, which writes the bare word. The tests of plain formulas below
record that state as it is; they do not say it is right. A formula with a
vector token or a status function never produces such a frame: it answers an
error instead (``test_vector_tokens.py``).
"""

import json
import math

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.formula_engine.app import app as formula_engine_app
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.vole.services.notifier import ChangeNotifier
from canopy.formula_engine.dependencies import (
    get_formula_service,
    get_subscription_manager,
)
from tests.canopy.formula_engine.conftest import FakeDataSource
from canopy.formula_engine.services.parser import Parser
from canopy.formula_engine.services.formula_service import FormulaService
from canopy.formula_engine.services.subscription_manager import SubscriptionManager

READ_HEADERS = {"X-API-Key": "test-read"}


def _strict(text: str) -> dict:
    """Parse as a browser does: a bare NaN or Infinity is an error."""

    def refuse(word: str):
        raise ValueError(f"{word} is not JSON")

    return json.loads(text, parse_constant=refuse)


@pytest.fixture
def client():
    source = FakeDataSource(
        latest={
            "DEV:NAN": Sample(ts_ns=1000, value=math.nan, severity=AlarmSeverity.NO_ALARM, source="live"),
            "DEV:ZERO": Sample(ts_ns=1000, value=0.0, severity=AlarmSeverity.NO_ALARM, source="live"),
        }
    )
    service = FormulaService(source, Parser(), FormulaEngineConfig())

    async def subscribe(pvs: list[str]) -> None:
        return None

    manager = SubscriptionManager(
        formula_service=service, on_change_fn=ChangeNotifier().subscribe, subscribe_live_fn=subscribe
    )
    formula_engine_app.dependency_overrides[get_formula_service] = lambda: service
    formula_engine_app.dependency_overrides[get_subscription_manager] = lambda: manager
    try:
        with TestClient(parent_app) as test_client:
            yield test_client
    finally:
        formula_engine_app.dependency_overrides.pop(get_formula_service, None)
        formula_engine_app.dependency_overrides.pop(get_subscription_manager, None)


PLAIN = [
    ("{a} * 1", {"a": "DEV:NAN"}, "NaN"),
    ("log({z})", {"z": "DEV:ZERO"}, "-Infinity"),
]
VECTOR = [
    ("max({*})", {"0": "DEV:NAN", "1": "DEV:ZERO"}),
    ("log(max({*}))", {"0": "DEV:ZERO"}),
    ("severity({a}) * {a}", {"a": "DEV:NAN"}),
]


def _first_frame(client: TestClient, expression: str, bindings: dict[str, str]) -> str:
    with client.websocket_connect("/formula-engine/v1/ws/formulas?api_key=test-read") as socket:
        socket.send_json(
            {"type": "subscribe", "spec": {"kind": "scalar", "expression": expression, "bindings": bindings}}
        )
        assert json.loads(socket.receive_text())["type"] == "subscribed"
        return socket.receive_text()


def _evaluate(client: TestClient, expression: str, bindings: dict[str, str]):
    spec = {"kind": "scalar", "expression": expression, "bindings": bindings}
    return client.post("/formula-engine/v1/evaluate", headers=READ_HEADERS, json={"spec": spec, "t0Ns": 0, "t1Ns": 0})


@pytest.mark.parametrize(("expression", "bindings", "word"), PLAIN)
def test_a_plain_formula_sends_the_bare_word_over_the_websocket(client, expression, bindings, word):
    """As it is today: this frame is not JSON, and a browser client drops it."""
    text = _first_frame(client, expression, bindings)
    assert f'"value":{word}' in text
    with pytest.raises(ValueError, match="is not JSON"):
        _strict(text)


@pytest.mark.parametrize(("expression", "bindings", "word"), PLAIN)
def test_a_plain_formula_answers_null_over_rest(client, expression, bindings, word):
    response = _evaluate(client, expression, bindings)
    assert response.status_code == 200
    assert _strict(response.text)["scalar"]["value"] is None


@pytest.mark.parametrize(("expression", "bindings"), VECTOR)
def test_a_vector_or_status_formula_never_sends_a_frame_that_is_not_json(client, expression, bindings):
    frame = _strict(_first_frame(client, expression, bindings))
    assert frame["type"] == "error"
    assert frame["code"] == "formula_type_error"
    assert "not a finite number" in frame["message"]


@pytest.mark.parametrize(("expression", "bindings"), VECTOR)
def test_a_vector_or_status_formula_answers_the_error_over_rest(client, expression, bindings):
    response = _evaluate(client, expression, bindings)
    assert response.status_code == 422
    assert _strict(response.text)["code"] == "formula_type_error"


def test_a_roll_up_over_rest_has_the_summary_and_a_plain_formula_does_not(client):
    roll_up = _evaluate(client, "sum(connected({*}))", {"0": "DEV:ZERO", "1": "DEV:GONE"}).json()["scalar"]
    assert roll_up["value"] == 1
    assert roll_up["inputsUsed"] == {}
    assert roll_up["vectorInputs"] == {"count": 2, "missing": ["DEV:GONE"], "more": 0}
    plain = _evaluate(client, "{z} + 1", {"z": "DEV:ZERO"}).json()["scalar"]
    assert plain == {"tsNs": 1000, "value": 1.0, "severity": "NO_ALARM", "inputsUsed": {"DEV:ZERO": 1000}}
