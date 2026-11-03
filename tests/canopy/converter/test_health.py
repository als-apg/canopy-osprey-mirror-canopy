"""Converter health endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.contracts.health import ConverterHealthSummaryDTO
from canopy.core.color_scheme import DEGRADED_KEY, clear_color_scheme_cache

SUMMARY = "/converter/v1/health/summary"
SCHEMES = Path(__file__).resolve().parents[2] / "_fixtures" / "schemes"
BAD_SCHEMES = [SCHEMES / "unparseable.json", SCHEMES / "dangling-alias.json"]
SCHEME_ENV = ("CANOPY_CONVERTER__SCHEME_PATH", "CANOPY_THEME_FILE", "CANOPY_CONVERTER__COLOR_DEF_PATH")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in SCHEME_ENV:
        monkeypatch.delenv(name, raising=False)
    clear_color_scheme_cache()
    yield
    clear_color_scheme_cache()


def test_watchdog_returns_heartbeat():
    with TestClient(app) as client:
        resp = client.get("/converter/v1/health/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["module"] == "converter"
    assert "version" in body
    assert body["uptimeS"] >= 0
    assert body["nowTsNs"] > 0


def test_summary_is_ok_while_the_scheme_loads():
    with TestClient(app) as client:
        resp = client.get(SUMMARY, headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "degraded": []}


@pytest.mark.parametrize("env", ["CANOPY_CONVERTER__SCHEME_PATH", "CANOPY_THEME_FILE"])
@pytest.mark.parametrize("path", BAD_SCHEMES, ids=lambda p: p.stem)
def test_summary_names_a_rejected_scheme(monkeypatch, path, env):
    monkeypatch.setenv(env, str(path))
    with TestClient(app) as client:
        resp = client.get(SUMMARY, headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "degraded": [DEGRADED_KEY]}


def test_the_summary_body_is_the_shared_contract():
    with TestClient(app) as client:
        resp = client.get(SUMMARY, headers=READ_HEADERS)
    parsed = ConverterHealthSummaryDTO.model_validate_json(resp.content, strict=True)
    assert parsed.model_dump(by_alias=True, mode="json") == resp.json()


def test_summary_requires_a_key():
    with TestClient(app) as client:
        resp = client.get(SUMMARY)
    assert resp.status_code == 401
