"""canopy-api reports the converter's color scheme on ``/health``; a bad scheme degrades, never 500s."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import canopy.converter.tasks as tasks_module
from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.core import color_scheme
from canopy.core.color_scheme import DEFAULT_ID, DEGRADED_KEY, clear_color_scheme_cache

SCHEMES = Path(__file__).resolve().parent.parent / "_fixtures" / "schemes"
BAD_SCHEMES = [SCHEMES / "unparseable.json", SCHEMES / "dangling-alias.json"]
EDL = (Path(__file__).parent / "converter" / "fixtures" / "min.edl").read_bytes()
SCHEME_ENV = ("CANOPY_CONVERTER__SCHEME_PATH", "CANOPY_THEME_FILE", "CANOPY_CONVERTER__COLOR_DEF_PATH")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in SCHEME_ENV:
        monkeypatch.delenv(name, raising=False)
    clear_color_scheme_cache()
    tasks_module._service = None
    yield
    clear_color_scheme_cache()
    tasks_module._service = None


def _errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == color_scheme.__name__ and r.levelno >= logging.ERROR]


def test_healthy_scheme_reports_empty_degraded():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "degraded": []}


@pytest.mark.parametrize("env", ["CANOPY_CONVERTER__SCHEME_PATH", "CANOPY_THEME_FILE"])
@pytest.mark.parametrize("path", BAD_SCHEMES, ids=lambda p: p.stem)
def test_bad_scheme_degrades_health_and_conversions(monkeypatch, caplog, path, env):
    monkeypatch.setenv(env, str(path))
    caplog.set_level(logging.ERROR, logger=color_scheme.__name__)
    with TestClient(app) as client:
        # The converter flattens at startup, before any request.
        assert len(_errors(caplog)) == 1
        health = client.get("/health")
        conversion = client.post(
            "/converter/v1/conversions",
            headers=READ_HEADERS,
            files={"file": ("min.edl", EDL, "application/octet-stream")},
        )
        client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "healthy", "degraded": [DEGRADED_KEY]}
    assert conversion.status_code == 200
    body = conversion.json()
    assert body["scheme"]["served"] == DEFAULT_ID
    assert body["scheme"]["errors"]
    assert len([w for w in body["warnings"] if w.startswith("Color scheme could not be loaded")]) == 1
    assert len(_errors(caplog)) == 1


def test_health_schema_is_typed():
    schema = app.openapi()["components"]["schemas"]["HealthDTO"]
    assert set(schema["properties"]) == {"status", "degraded"}
