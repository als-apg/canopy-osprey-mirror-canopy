"""canopy_mcp flattens the color scheme at startup and reports a bad one on ``/health``."""

import logging
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy.core import color_scheme
from canopy_mcp.app import create_app
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from canopy.core.color_scheme import DEFAULT_ID, DEGRADED_KEY, clear_color_scheme_cache
from tests.canopy_mcp.conftest import summary

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
SCHEMES = Path(__file__).resolve().parent.parent / "_fixtures" / "schemes"
BAD_SCHEMES = [SCHEMES / "unparseable.json", SCHEMES / "dangling-alias.json"]


@pytest.fixture(autouse=True)
def _fresh_scheme(monkeypatch):
    monkeypatch.delenv("CANOPY_THEME_FILE", raising=False)
    clear_color_scheme_cache()
    yield
    clear_color_scheme_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    drafts = tmp_path / "drafts"
    (drafts / "screens").mkdir(parents=True)
    return Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, pv_mode="mock")


def _errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == color_scheme.__name__ and r.levelno >= logging.ERROR]


def test_mock_mode_answers_ok_with_no_channel_keys(settings):
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", BAD_SCHEMES, ids=lambda p: p.stem)
def test_bad_scheme_degrades_health_and_logs_once(settings, monkeypatch, caplog, path):
    monkeypatch.setenv("CANOPY_THEME_FILE", str(path))
    caplog.set_level(logging.ERROR, logger=color_scheme.__name__)
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        # The flatten happens at startup, before any request.
        assert len(_errors(caplog)) == 1
        health = client.get("/health")
        config = client.get("/config.json")
        client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "degraded": [DEGRADED_KEY]}
    meta = config.json()["colorScheme"]["meta"]
    assert meta["served"] == DEFAULT_ID
    assert meta["errors"]
    assert len(_errors(caplog)) == 1


HARE = dict(pv_mode="hare", api_key="sekrit-key")


def _json(body, status: int = 200):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return respond


def _health(settings: Settings, respond) -> dict:
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        if respond is not None:
            c.app.state.http = make_client(settings, transport=httpx.MockTransport(respond))
        return c.get("/health").json()


@pytest.mark.real_data_summary
def test_hare_mode_reports_the_connected_channels(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(summary(connected=53, active_monitors=53, subscriptions=53))) == {
        "status": "ok",
        "connected": 53,
        "total": 53,
    }


@pytest.mark.real_data_summary
def test_hare_mode_is_degraded_when_none_of_the_monitored_channels_is_connected(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(summary(connected=0, active_monitors=53, subscriptions=53))) == {
        "status": "degraded",
        "connected": 0,
        "total": 53,
    }


@pytest.mark.real_data_summary
def test_no_monitors_is_not_degraded(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(summary(connected=0, active_monitors=0, subscriptions=0))) == {
        "status": "ok",
        "connected": 0,
        "total": 0,
    }


@pytest.mark.real_data_summary
def test_hare_mode_without_data_is_degraded_and_still_200(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        c.app.state.http = make_client(settings, transport=httpx.MockTransport(_json({"detail": "down"}, 503)))
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "data": "unavailable"}


NOT_A_SUMMARY = [
    {**summary(), "connected": False},
    {**summary(), "activeMonitors": True},
    {**summary(), "connected": "0"},
    {**summary(), "connected": None},
    {k: v for k, v in summary().items() if k != "connected"},
]
NOT_A_SUMMARY_IDS = ["bool-connected", "bool-active-monitors", "string-connected", "null-connected", "no-connected"]


@pytest.mark.real_data_summary
@pytest.mark.parametrize("body", NOT_A_SUMMARY, ids=NOT_A_SUMMARY_IDS)
def test_a_body_that_is_not_the_summary_makes_data_unavailable(tmp_path, body):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(body)) == {"status": "degraded", "data": "unavailable"}


@pytest.mark.real_data_summary
def test_an_unreadable_summary_is_logged_by_the_fetch_not_by_a_route(tmp_path, caplog):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    caplog.set_level(logging.DEBUG, logger="canopy_mcp.static")
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        c.app.state.http = make_client(settings, transport=httpx.MockTransport(_json({**summary(), "connected": "0"})))
        for path in ("/health", "/config.json"):
            caplog.clear()
            c.get(path)
            records = [r for r in caplog.records if r.name == "canopy_mcp.static"]
            assert len(records) == 1, path
            message = records[0].getMessage()
            assert message.startswith("DATA summary unavailable: ")
            assert "config.json" not in message
            assert "/health" not in message
