"""/config.json carries the active color scheme and may be fetched cross-origin."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp import static
from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy.core.color_scheme import (
    ColorScheme,
    get_color_scheme,
    clear_color_scheme_cache,
)

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"


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


def _get(settings: Settings):
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        return c.get("/config.json")


def test_unset_theme_file_serves_flat_canopy_default(settings):
    r = _get(settings)
    assert r.status_code == 200
    scheme = r.json()["colorScheme"]
    assert scheme == get_color_scheme().flat_json
    assert scheme["meta"]["id"] == "canopy-default"
    assert scheme["meta"]["served"] == "canopy-default"
    assert scheme["meta"]["errors"] == []
    assert "Text" in scheme["roles"]


def test_theme_file_env_selects_the_scheme(settings, tmp_path, monkeypatch):
    path = tmp_path / "facility.json"
    path.write_text(json.dumps({"id": "facility-x", "extends": "canopy-default", "roles": {"Beam": "#123456"}}))
    monkeypatch.setenv("CANOPY_THEME_FILE", str(path))
    scheme = _get(settings).json()["colorScheme"]
    assert scheme["meta"]["id"] == "facility-x"
    assert "Beam" in scheme["roles"]


def test_broken_theme_file_serves_default_with_errors(settings, tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    monkeypatch.setenv("CANOPY_THEME_FILE", str(path))
    scheme = _get(settings).json()["colorScheme"]
    assert scheme["meta"]["served"] == "canopy-default"
    assert scheme["meta"]["errors"]


def test_missing_flattener_omits_color_scheme(settings, monkeypatch):
    unavailable = ColorScheme(result=None, flat_json=None, meta={}, imports={}, degraded=["color-scheme"])
    monkeypatch.setattr(static, "get_color_scheme", lambda: unavailable)
    body = _get(settings).json()
    assert "colorScheme" not in body
    assert {"pvMode", "screensPath", "themes"} <= set(body)


def test_config_json_allows_any_origin(settings):
    r = _get(settings)
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["cache-control"] == "no-cache"


def test_viewer_config_has_no_converter_claim(settings):
    config = static.viewer_config(settings)
    assert not any("converter" in k.lower() for k in config)
    assert not any("key" in k.lower() for k in config)
