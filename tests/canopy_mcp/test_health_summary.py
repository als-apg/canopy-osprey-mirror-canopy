"""canopy_mcp's two health routes: the open liveness body and the credentialed rollup on ``/health/summary``."""

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy.contracts import load_heartbeat_vectors
from canopy_mcp.access import ACCESS_HEADER
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from canopy.contracts.health import DATA_DEGRADED, DATA_UNAVAILABLE, HealthDTO
from canopy.core.color_scheme import DEGRADED_KEY, clear_color_scheme_cache
from tests.canopy_mcp.conftest import summary

SCHEMES = Path(__file__).resolve().parent.parent / "_fixtures" / "schemes"
TOKEN = "launch-credential-7f3a"
ACCESS = {ACCESS_HEADER: TOKEN}


@pytest.fixture(autouse=True)
def _fresh_scheme(monkeypatch):
    monkeypatch.delenv("CANOPY_THEME_FILE", raising=False)
    clear_color_scheme_cache()
    yield
    clear_color_scheme_cache()


class Data:
    """A DATA double: answers every request with one body and records the paths asked."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        return httpx.Response(self.status, json=self.body)


def _settings(tmp_path: Path, *, mode: str = "hare", gated: bool = False) -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    token_file = None
    if gated:
        token_file = tmp_path / "front.access"
        token_file.write_text(TOKEN + "\n")
    return Settings(drafts_dir=drafts, api_key="sekrit-key", pv_mode=mode, access_token_file=token_file)


def _get(settings: Settings, data: Data, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        c.app.state.http = make_client(settings, transport=httpx.MockTransport(data))
        return c.get(path, headers=headers or {})


@pytest.mark.real_data_summary
def test_the_rollup_carries_the_counts(tmp_path):
    data = Data(summary(connected=53, active_monitors=53))
    resp = _get(_settings(tmp_path), data, "/health/summary")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "degraded": [],
        "channels": {"connected": 53, "total": 53},
        "reachable": None,
    }
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.real_data_summary
def test_the_rollup_relays_datas_verdict(tmp_path):
    data = Data(summary(status="degraded", connected=0, active_monitors=53))
    assert _get(_settings(tmp_path), data, "/health/summary").json() == {
        "status": "degraded",
        "degraded": [DATA_DEGRADED],
        "channels": {"connected": 0, "total": 53},
        "reachable": None,
    }


@pytest.mark.real_data_summary
@pytest.mark.parametrize(
    ("overrides", "status", "degraded"),
    [
        (dict(status="ok", connected=0, active_monitors=53), "ok", []),
        (dict(status="degraded", connector_ok=False, connected=0, active_monitors=0), "degraded", [DATA_DEGRADED]),
    ],
    ids=["ok-with-none-connected", "degraded-with-no-monitors"],
)
def test_the_rollup_does_not_rederive_the_verdict_from_the_counts(tmp_path, overrides, status, degraded):
    body = _get(_settings(tmp_path), Data(summary(**overrides)), "/health/summary").json()
    assert body["status"] == status
    assert body["degraded"] == degraded


@pytest.mark.real_data_summary
def test_the_rollup_without_data_is_degraded_and_still_200(tmp_path):
    resp = _get(_settings(tmp_path), Data({"detail": "down"}, 503), "/health/summary")
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "degraded": [DATA_UNAVAILABLE], "channels": None, "reachable": None}


@pytest.mark.real_data_summary
def test_the_rollup_names_a_bad_scheme_first(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", str(SCHEMES / "unparseable.json"))
    data = Data(summary(status="degraded", connected=0, active_monitors=53))
    body = _get(_settings(tmp_path), data, "/health/summary").json()
    assert body["status"] == "degraded"
    assert body["degraded"] == [DEGRADED_KEY, DATA_DEGRADED]


@pytest.mark.real_data_summary
def test_mock_mode_rollup_never_asks_data(tmp_path):
    data = Data(summary())
    resp = _get(_settings(tmp_path, mode="mock"), data, "/health/summary")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "degraded": [], "channels": None, "reachable": None}
    assert data.requests == []


@pytest.mark.real_data_summary
def test_the_rollup_carries_nothing_else_of_the_summary(tmp_path):
    data = Data(
        summary(
            label="Simulator",
            notice="not the machine",
            binding="va@3",
            writes={"state": "locked", "reason": "writes held by the persona", "cause": "ceiling"},
        )
    )
    resp = _get(_settings(tmp_path), data, "/health/summary")
    assert set(resp.json()) == {"status", "degraded", "channels", "reachable"}
    for leaked in ("Simulator", "not the machine", "va@3", "writes", "locked", "persona", "ceiling"):
        assert leaked not in resp.text


REACHABLE_NO = {"state": "no", "since": 1760699400.0, "reason": None}


@pytest.mark.real_data_summary
@pytest.mark.parametrize(
    "reachable",
    [
        REACHABLE_NO,
        {"state": "yes", "since": 1760699400.5, "reason": None},
        {"state": "unknown", "since": None, "reason": "no name server"},
    ],
    ids=["no", "yes", "unknown"],
)
def test_the_rollup_relays_reachable_beside_the_channels(tmp_path, reachable):
    data = Data(summary(connected=4, active_monitors=4, reachable=reachable))
    body = _get(_settings(tmp_path), data, "/health/summary").json()
    assert body["channels"] == {"connected": 4, "total": 4}
    assert body["reachable"] == reachable


@pytest.mark.real_data_summary
def test_an_unreachable_machine_is_data_degraded_once(tmp_path):
    data = Data(summary(status="degraded", connected=4, active_monitors=4, reachable=REACHABLE_NO))
    assert _get(_settings(tmp_path), data, "/health/summary").json() == {
        "status": "degraded",
        "degraded": [DATA_DEGRADED],
        "channels": {"connected": 4, "total": 4},
        "reachable": REACHABLE_NO,
    }


@pytest.mark.real_data_summary
def test_a_malformed_reachable_makes_datas_summary_unavailable(tmp_path):
    """DATA's summary is read strictly: a malformed nested ``reachable`` is not the summary."""
    data = Data({**summary(connected=4, active_monitors=4), "reachable": {"since": 1}})
    assert _get(_settings(tmp_path), data, "/health/summary").json() == {
        "status": "degraded",
        "degraded": [DATA_UNAVAILABLE],
        "channels": None,
        "reachable": None,
    }


@pytest.mark.real_data_summary
def test_mock_mode_rollup_carries_no_reachable(tmp_path):
    data = Data(summary(reachable=REACHABLE_NO))
    assert _get(_settings(tmp_path, mode="mock"), data, "/health/summary").json()["reachable"] is None


@pytest.mark.real_data_summary
@pytest.mark.parametrize("case", load_heartbeat_vectors()["summaries"], ids=lambda c: c["case"])
def test_the_rollup_reads_every_vector_summary(tmp_path, case):
    body = _get(_settings(tmp_path), Data(case["summary"]), "/health/summary").json()
    summary_in = case["summary"]
    assert body["channels"] == {"connected": summary_in["connected"], "total": summary_in["active_monitors"]}
    assert body["reachable"] == case["expected"]["reachable"]
    assert body["degraded"] == ([DATA_DEGRADED] if summary_in["status"] == "degraded" else [])
    assert "writes" not in body


@pytest.mark.real_data_summary
def test_the_rollup_needs_the_credential(tmp_path):
    settings = _settings(tmp_path, gated=True)
    data = Data(summary(connected=2, active_monitors=3))
    refused = _get(settings, data, "/health/summary")
    assert refused.status_code == 401
    assert data.requests == []
    admitted = _get(settings, data, "/health/summary", ACCESS)
    assert admitted.status_code == 200
    assert admitted.json() == {
        "status": "ok",
        "degraded": [],
        "channels": {"connected": 2, "total": 3},
        "reachable": None,
    }


@pytest.mark.real_data_summary
def test_the_rollup_is_open_where_no_credential_is_configured(tmp_path):
    resp = _get(_settings(tmp_path), Data(summary(connected=2, active_monitors=3)), "/health/summary")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "degraded": [], "channels": {"connected": 2, "total": 3}, "reachable": None}


@pytest.mark.real_data_summary
def test_open_health_asks_data_nothing(tmp_path):
    data = Data(summary(connected=53, active_monitors=53))
    resp = _get(_settings(tmp_path), data, "/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}
    assert data.requests == []


@pytest.mark.real_data_summary
@pytest.mark.parametrize("gated", [True, False], ids=["gated", "ungated"])
@pytest.mark.parametrize("mode", ["mock", "hare"])
@pytest.mark.parametrize("status", [200, 503], ids=["data-up", "data-503"])
def test_open_health_is_one_body_everywhere(tmp_path, gated, mode, status):
    data = Data(summary(status="degraded", connected=0, active_monitors=53), status)
    resp = _get(_settings(tmp_path, mode=mode, gated=gated), data, "/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_open_health_is_the_apis_body(tmp_path):
    resp = _get(_settings(tmp_path), Data(summary()), "/health")
    assert resp.json() == HealthDTO.of().model_dump(by_alias=True, mode="json")


def test_open_health_does_not_report_a_bad_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", str(SCHEMES / "unparseable.json"))
    assert _get(_settings(tmp_path, mode="mock"), Data(summary()), "/health").json() == {"status": "healthy"}
