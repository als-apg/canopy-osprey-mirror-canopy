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
from canopy_mcp.static import channel_health
from canopy.core.color_scheme import DEFAULT_ID, DEGRADED_KEY, clear_color_scheme_cache

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


def test_healthy_body_is_unchanged(settings):
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


def _summary(connected: int, total: int) -> dict:
    return {
        "status": "ok",
        "backend": "osprey",
        "connectorOk": True,
        "clients": 1,
        "activeMonitors": total,
        "connected": connected,
        "warmMonitors": 0,
        "subscriptions": total,
        "label": None,
        "notice": None,
        "binding": "va@3",
    }


def _health(settings: Settings, respond) -> dict:
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        if respond is not None:
            c.app.state.http = make_client(settings, transport=httpx.MockTransport(respond))
        return c.get("/health").json()


@pytest.mark.real_data_summary
def test_hare_mode_reports_the_connected_channels(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(_summary(53, 53))) == {"status": "ok", "connected": 53, "total": 53}


@pytest.mark.real_data_summary
def test_hare_mode_is_degraded_when_none_of_the_monitored_channels_is_connected(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(_summary(0, 53))) == {"status": "degraded", "connected": 0, "total": 53}


@pytest.mark.real_data_summary
def test_no_monitors_is_not_degraded(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    assert _health(settings, _json(_summary(0, 0))) == {"status": "ok", "connected": 0, "total": 0}


@pytest.mark.real_data_summary
def test_hare_mode_without_data_is_degraded_and_still_200(tmp_path):
    settings = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, **HARE)
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        c.app.state.http = make_client(settings, transport=httpx.MockTransport(_json({"detail": "down"}, 503)))
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "data": "unavailable"}


@pytest.mark.parametrize(("connected", "total"), [(False, 53), (0, True), ("0", 53), (None, 53)])
def test_a_summary_whose_counts_are_not_integers_adds_no_channel_keys(connected, total):
    assert channel_health({"connected": connected, "activeMonitors": total}) == {}
