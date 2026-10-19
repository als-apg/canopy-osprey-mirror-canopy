"""Hare health endpoints."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.contracts import load_heartbeat_vectors
from canopy.hare.api.v1 import health as health_module
from canopy.core.security import require_read_access
from canopy.contracts.health import HareHealthSummaryDTO
from canopy.hare.dependencies import get_pv_broker


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


def _summary(broker: _Rollup) -> dict:
    app = FastAPI()
    app.include_router(health_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: broker
    app.dependency_overrides[require_read_access] = lambda: None
    with TestClient(app) as client:
        resp = client.get("/health/summary")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.parametrize(
    ("monitored", "connected", "status"),
    [(0, 0, "ok"), (53, 53, "ok"), (53, 1, "ok"), (53, 0, "degraded")],
)
def test_summary_is_degraded_only_when_monitors_exist_and_none_is_connected(monitored, connected, status):
    app = FastAPI()
    app.include_router(health_module.router)
    app.dependency_overrides[get_pv_broker] = lambda: _Rollup(monitored, connected)
    app.dependency_overrides[require_read_access] = lambda: None
    with TestClient(app) as client:
        body = client.get("/health/summary").json()
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
    health_module._last_malformed.clear()
    caplog.set_level("WARNING", logger=health_module.__name__)
    broker = _Rollup(53, 0, statement={"reachable": bad, "writes": WRITES_LOCKED})
    first, second = _summary(broker), _summary(broker)
    assert first == second
    assert first["status"] == "degraded"
    assert first["reachable"] is None
    assert first["writes"] == WRITES_LOCKED
    assert _summary(_Rollup(53, 1, statement={"reachable": bad}))["status"] == "ok"
    # One bad statement repeated on every poll logs once.
    assert len([r for r in caplog.records if "reachable statement is malformed" in r.getMessage()]) == 1


@pytest.mark.parametrize("bad", MALFORMED)
def test_a_malformed_writes_is_dropped_and_the_summary_still_answers(bad):
    health_module._last_malformed.clear()
    body = _summary(_Rollup(4, 4, statement={"reachable": REACHABLE["yes"], "writes": bad}))
    assert body["status"] == "ok"
    assert body["reachable"] == REACHABLE["yes"]
    assert body["writes"] is None
