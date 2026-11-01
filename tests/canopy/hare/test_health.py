"""Hare health endpoints."""

import json
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.contracts import load_heartbeat_vectors
from canopy.hare.api.v1 import health as health_module
from canopy.hare.config import HareConfig
from canopy.core.security import require_read_access
from canopy.contracts.health import HareHealthSummaryDTO
from canopy.hare.dependencies import get_pv_broker
from tests.canopy.hare.conftest import FakeConnector, FakeWebSocket
from canopy.hare.services.broker import PVBroker


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "hare"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0


def test_summary_reports_backend():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["backend"] == "simulation"
    assert body["connectorOk"] is True
    assert body["warmMonitors"] == 0
    assert body["connected"] == 0
    # The built-in simulation connector exposes no identity: all three null.
    assert body["label"] is None
    assert body["notice"] is None
    assert body["binding"] is None
    # Nor any machine statement: both null, and the count rule decides.
    assert body["reachable"] is None
    assert body["writes"] is None


def test_the_summary_body_is_the_shared_contract():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    parsed = HareHealthSummaryDTO.model_validate_json(resp.content, strict=True)
    assert parsed.model_dump(by_alias=True, mode="json") == resp.json()


def test_summary_requires_a_key():
    with TestClient(app) as client:
        resp = client.get("/hare/v1/health/summary")
    assert resp.status_code == 401


class _Rollup:
    """A broker standing in by the two methods the summary reads."""

    def __init__(
        self,
        active_monitors: int,
        connected: int,
        *,
        running: bool = True,
        statement: dict | None = None,
    ) -> None:
        self.active_monitors, self.connected = active_monitors, connected
        self.running = running
        self.statement = statement or {}

    def status(self) -> dict:
        return {
            "running": self.running,
            "backend": "fake",
            "clients": 1,
            "active_monitors": self.active_monitors,
            "warm_monitors": 0,
            "connected": self.connected,
            "active_pvs": self.active_monitors,
            "subscriptions": self.active_monitors,
        }

    def heartbeat_frame(self) -> dict:
        return {
            "type": "heartbeat",
            "timestamp": 0.0,
            "backend": "fake",
            "label": None,
            "notice": None,
            "binding": None,
            **self.statement,
        }


def _app(broker) -> FastAPI:
    app = FastAPI()
    app.include_router(health_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: broker
    app.dependency_overrides[require_read_access] = lambda: None
    return app


def _summary(broker: _Rollup, *, app: FastAPI | None = None) -> dict:
    with TestClient(app or _app(broker)) as client:
        resp = client.get("/health/summary")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.parametrize(
    ("monitored", "connected", "status"),
    [(0, 0, "ok"), (53, 53, "ok"), (53, 1, "ok"), (53, 0, "degraded")],
)
def test_summary_is_degraded_only_when_monitors_exist_and_none_is_connected(monitored, connected, status):
    body = _summary(_Rollup(monitored, connected))
    assert (body["status"], body["activeMonitors"], body["connected"]) == (status, monitored, connected)
    assert body["connectorOk"] is True


REACHABLE = {
    "yes": {"state": "yes", "since": 1760699400.0, "reason": None},
    "no": {"state": "no", "since": 1760699400.0, "reason": None},
    "unknown": {"state": "unknown", "since": None, "reason": "this connector has no name server to ask"},
}
WRITES_LOCKED = {"state": "locked", "reason": "writes are locked", "cause": "ceiling"}


@pytest.mark.parametrize(("monitored", "connected"), [(0, 0), (53, 53), (53, 0)])
def test_a_broker_that_is_not_running_is_degraded(monitored, connected):
    body = _summary(_Rollup(monitored, connected, running=False, statement={"reachable": REACHABLE["yes"]}))
    assert body["status"] == "degraded"
    assert body["connectorOk"] is False


@pytest.mark.parametrize(("monitored", "connected"), [(0, 0), (53, 53), (53, 1)])
def test_an_unreachable_machine_is_degraded_whatever_the_counts(monitored, connected):
    body = _summary(_Rollup(monitored, connected, statement={"reachable": REACHABLE["no"]}))
    assert body["status"] == "degraded"
    assert body["reachable"] == REACHABLE["no"]


@pytest.mark.parametrize("state", ["yes", "unknown"])
def test_a_reachable_claim_other_than_no_overrides_the_count_rule(state):
    body = _summary(_Rollup(53, 0, statement={"reachable": REACHABLE[state]}))
    assert body["status"] == "ok"
    assert body["reachable"] == REACHABLE[state]


@pytest.mark.parametrize(
    ("monitored", "connected", "status"),
    [(0, 0, "ok"), (53, 1, "ok"), (53, 0, "degraded")],
)
def test_a_null_reachable_falls_back_to_the_count_rule(monitored, connected, status):
    body = _summary(_Rollup(monitored, connected, statement={"reachable": None, "writes": None}))
    assert body["status"] == status
    assert body["reachable"] is None
    assert body["writes"] is None


@pytest.mark.parametrize("state", ["on", "off", "locked", "applying"])
def test_writes_never_enters_the_verdict(state):
    writes = {"state": state, "reason": None, "cause": None}
    assert _summary(_Rollup(53, 53, statement={"reachable": None, "writes": writes}))["status"] == "ok"
    body = _summary(_Rollup(53, 0, statement={"reachable": REACHABLE["yes"], "writes": writes}))
    assert body["status"] == "ok"
    assert body["writes"] == writes


def test_the_summary_copies_both_statements_from_the_frame():
    body = _summary(_Rollup(4, 4, statement={"reachable": REACHABLE["unknown"], "writes": WRITES_LOCKED}))
    assert body["reachable"] == REACHABLE["unknown"]
    assert body["writes"] == WRITES_LOCKED
    parsed = HareHealthSummaryDTO.model_validate_json(json.dumps(body), strict=True)
    assert parsed.model_dump(by_alias=True, mode="json") == body


@pytest.mark.parametrize("case", load_heartbeat_vectors()["summaries"], ids=lambda c: c["case"])
def test_the_dto_reads_every_vector_summary(case):
    parsed = HareHealthSummaryDTO.model_validate_json(json.dumps(case["summary"]), strict=True)
    dumped = parsed.model_dump(mode="json")
    for key, expected in case["expected"].items():
        assert dumped[key] == expected


MALFORMED = [
    pytest.param({"since": 1760699400.0}, id="no-state"),
    pytest.param({"state": 3}, id="non-string-state"),
    pytest.param("no", id="not-an-object"),
]


@pytest.mark.parametrize("bad", MALFORMED)
def test_a_malformed_reachable_is_no_claim_and_the_count_rule_decides(bad, caplog):
    caplog.set_level("WARNING", logger=health_module.__name__)
    broker = _Rollup(53, 0, statement={"reachable": bad, "writes": WRITES_LOCKED})
    app = _app(broker)
    first, second = _summary(broker, app=app), _summary(broker, app=app)
    assert first == second
    assert first["status"] == "degraded"
    assert first["reachable"] is None
    assert first["writes"] == WRITES_LOCKED
    # One bad statement repeated on every poll logs once.
    assert len([r for r in caplog.records if "reachable statement is malformed" in r.getMessage()]) == 1
    assert _summary(_Rollup(53, 1, statement={"reachable": bad}))["status"] == "ok"


@pytest.mark.parametrize("bad", MALFORMED)
def test_a_malformed_writes_is_dropped_and_the_summary_still_answers(bad):
    body = _summary(_Rollup(4, 4, statement={"reachable": REACHABLE["yes"], "writes": bad}))
    assert body["status"] == "ok"
    assert body["reachable"] == REACHABLE["yes"]
    assert body["writes"] is None


def test_the_malformed_log_once_is_per_app(caplog):
    caplog.set_level("WARNING", logger=health_module.__name__)
    broker = _Rollup(53, 0, statement={"reachable": "no"})
    _summary(broker, app=_app(broker))
    _summary(broker, app=_app(broker))
    assert len([r for r in caplog.records if "reachable statement is malformed" in r.getMessage()]) == 2


def test_a_malformed_statement_is_logged_again_after_a_good_one(caplog):
    caplog.set_level("WARNING", logger=health_module.__name__)
    broker = _Rollup(4, 4, statement={"reachable": "no"})
    app = _app(broker)
    _summary(broker, app=app)
    broker.statement = {"reachable": REACHABLE["yes"]}
    _summary(broker, app=app)
    broker.statement = {"reachable": "no"}
    _summary(broker, app=app)
    assert len([r for r in caplog.records if "reachable statement is malformed" in r.getMessage()]) == 2


# --- the summary against a real broker ----------------------------------------- #


@dataclass
class _StatementConnector(FakeConnector):
    label: str | None = "Test ring"
    notice: str | None = "fallback"
    binding: str | None = "ring@3"
    reachable: dict | None = None
    writes: dict | None = None

    def statement_sent(self) -> None:
        pass


@pytest.mark.parametrize(
    ("connector", "expected"),
    [
        (_StatementConnector(), ("Test ring", "fallback", "ring@3")),
        (
            _StatementConnector(label="Linac", notice="unservable", binding="unservable"),
            ("Linac", "unservable", "unservable"),
        ),
        (FakeConnector(), (None, None, None)),
    ],
)
def test_summary_carries_the_frame_identity(connector, expected):
    broker = PVBroker(connector, HareConfig(heartbeat_interval_s=100.0))
    body = _summary(broker)
    assert (body["label"], body["notice"], body["binding"]) == expected
    frame = broker.heartbeat_frame()
    assert body["backend"] == frame["backend"] == "fake"
    assert (body["label"], body["notice"], body["binding"]) == (frame["label"], frame["notice"], frame["binding"])


async def test_status_counts_connected_monitors(fake_connector):
    broker = PVBroker(fake_connector, HareConfig())
    broker.add_client("c1", FakeWebSocket())
    assert broker.status()["connected"] == 0
    await broker.subscribe("c1", ["PV:A", "PV:B"])
    await broker._drain_nowait()
    assert (broker.status()["active_monitors"], broker.status()["connected"]) == (2, 2)

    fake_connector.emit("PV:B", None, connected=False)
    await broker._drain_nowait()
    assert (broker.status()["active_monitors"], broker.status()["connected"]) == (2, 1)

    fake_connector.emit("PV:A", None, connected=False)
    await broker._drain_nowait()
    assert (broker.status()["active_monitors"], broker.status()["connected"]) == (2, 0)
    await broker.stop()


async def test_a_monitor_with_no_value_yet_is_not_connected(fake_connector):
    fake_connector.dead.add("PV:SILENT")
    broker = PVBroker(fake_connector, HareConfig())
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:SILENT"])
    await broker._drain_nowait()
    assert (broker.status()["active_monitors"], broker.status()["connected"]) == (1, 0)
    await broker.stop()
