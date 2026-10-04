"""canopy_mcp flattens the color scheme at startup and reports a bad one on ``/health``."""

import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy.core import color_scheme
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
    return Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST)


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
