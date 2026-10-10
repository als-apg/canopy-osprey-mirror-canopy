"""Module selection with the five modules that need no database or job queue.

Everything here must hold on an install without the ``data`` extra, so the file
imports nothing from the unselected modules and nothing those modules depend on.
The all-eleven cases live in ``test_module_selection_default.py``.
"""

import os
import sys
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from canopy.main import build_app
from tests.conftest import READ_HEADERS
from canopy.core.config import ALL_MODULES, Settings, parse_modules, resolve_modules

FIVE = ("hare", "vole", "fox", "mole", "converter")
FIVE_IN_ORDER = ("mole", "hare", "vole", "fox", "converter")
UNSELECTED = tuple(name for name in ALL_MODULES if name not in FIVE)
DATA_PACKAGES = ("arq", "sqlalchemy", "asyncpg", "alembic")


def _mount_paths(app) -> list[str]:
    return [route.path for route in app.routes if type(route).__name__ == "Mount"]


def _run_python(code: str, tmp_path, **env) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter, where ``canopy.main`` has not been imported yet."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def five_app():
    return build_app(FIVE)


# --- Parsing the setting -----------------------------------------------------------


def test_modules_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CANOPY_MODULES", "hare,vole,fox,mole,converter")
    assert Settings().modules == "hare,vole,fox,mole,converter"


def test_parse_orders_names_for_composition_whatever_the_input_order():
    assert parse_modules("hare,vole,fox,mole,converter") == FIVE_IN_ORDER


def test_parse_tolerates_spaces_blanks_and_repeats():
    assert parse_modules(" fox , hare,,vole,hare,mole,converter, ") == FIVE_IN_ORDER


@pytest.mark.parametrize("value", ["", " ", ","])
def test_an_empty_selection_means_every_module(value):
    assert parse_modules(value) == ALL_MODULES


def test_an_unknown_name_is_rejected_and_named():
    with pytest.raises(ValueError, match="badger"):
        resolve_modules(["hare", "badger"])


def test_a_name_is_matched_exactly():
    with pytest.raises(ValueError, match="Hare"):
        parse_modules("Hare")


# --- Composition ---------------------------------------------------------------------


def test_only_the_selected_modules_are_mounted_in_composition_order(five_app):
    assert _mount_paths(five_app) == [f"/{name}" for name in FIVE_IN_ORDER]
    assert five_app.state.modules == FIVE_IN_ORDER


def test_build_app_takes_the_comma_separated_form_too():
    assert _mount_paths(build_app("converter,hare")) == ["/hare", "/converter"]


def test_build_app_refuses_an_unknown_name():
    with pytest.raises(ValueError, match="badger"):
        build_app(["hare", "badger"])


def test_the_five_module_app_starts_and_serves(five_app):
    with TestClient(five_app) as client:
        assert client.get("/health").json() == {"status": "healthy", "degraded": []}
        assert client.get("/").json()["name"] == "Canopy"
        assert client.get("/converter/v1/health/watchdog").status_code == 200


@pytest.mark.parametrize("name", UNSELECTED)
def test_an_unselected_module_has_no_routes(five_app, name):
    client = TestClient(five_app)
    assert client.get(f"/{name}/openapi.json", headers=READ_HEADERS).status_code == 404


# --- Converter batch routes without Jobs ----------------------------------------------


def test_batch_submit_answers_503_when_jobs_is_not_selected(five_app):
    client = TestClient(five_app)
    resp = client.post(
        "/converter/v1/conversions/batch",
        headers=READ_HEADERS,
        files={"file": ("screens.zip", b"PK\x03\x04", "application/zip")},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "module_unavailable"
    assert "jobs" in body["detail"]


def test_batch_screen_fetch_answers_503_when_jobs_is_not_selected(five_app):
    client = TestClient(five_app)
    resp = client.get("/converter/v1/conversions/batch/abc/screens/0", headers=READ_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["code"] == "module_unavailable"


def test_batch_routes_still_ask_for_a_key_first(five_app):
    client = TestClient(five_app)
    resp = client.get("/converter/v1/conversions/batch/abc/screens/0")
    assert resp.status_code in (401, 403)


def test_the_batch_503_is_documented(five_app):
    from canopy.converter.app import app as converter_app

    paths = converter_app.openapi()["paths"]
    assert "503" in paths["/v1/conversions/batch"]["post"]["responses"]
    assert "503" in paths["/v1/conversions/batch/{job_id}/screens/{index}"]["get"]["responses"]


# --- Startup in a fresh interpreter ----------------------------------------------------

_REPORT_LOADED = """
import sys, json
import canopy.main
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("canopy", "arq", "sqlalchemy", "asyncpg", "alembic"))
print(json.dumps({"mounts": [r.path for r in canopy.main.app.routes if type(r).__name__ == "Mount"], "loaded": loaded}))
"""


def test_canopy_modules_selects_what_canopy_main_app_serves(tmp_path):
    proc = _run_python(_REPORT_LOADED, tmp_path, CANOPY_MODULES=",".join(FIVE))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["mounts"] == [f"/{name}" for name in FIVE_IN_ORDER]


# An install without the ``data`` extra: SQLAlchemy cannot be imported. The
# blocking entry is dropped before the report so it is not listed as loaded.
_REPORT_LOADED_LEAN = (
    "import sys\nsys.modules['sqlalchemy'] = None\nimport canopy.main\ndel sys.modules['sqlalchemy']\n" + _REPORT_LOADED
)


def test_unselected_modules_and_their_packages_are_never_imported(tmp_path):
    proc = _run_python(_REPORT_LOADED_LEAN, tmp_path, CANOPY_MODULES=",".join(FIVE))
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])["loaded"]

    assert "canopy.converter.app" in loaded
    stray_apps = [f"canopy.{name}.app" for name in UNSELECTED if f"canopy.{name}.app" in loaded]
    assert stray_apps == []
    assert "canopy.core.db" not in loaded
    assert "canopy.jobs.gateway" not in loaded
    stray_packages = [m for m in loaded if m.split(".")[0] in DATA_PACKAGES]
    assert stray_packages == []


def test_with_sqlalchemy_installed_only_the_key_store_is_added(tmp_path):
    """The env + database key chain is core, so SQLAlchemy loads; no unselected module or job queue does."""
    proc = _run_python(_REPORT_LOADED, tmp_path, CANOPY_MODULES=",".join(FIVE))
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])["loaded"]

    stray_apps = [f"canopy.{name}.app" for name in UNSELECTED if f"canopy.{name}.app" in loaded]
    assert stray_apps == []
    assert "canopy.jobs.gateway" not in loaded
    assert [m for m in loaded if m.split(".")[0] in ("arq", "alembic")] == []


def test_an_unknown_name_in_canopy_modules_fails_startup(tmp_path):
    proc = _run_python("import canopy.main", tmp_path, CANOPY_MODULES="hare,badger")
    assert proc.returncode != 0
    assert "badger" in proc.stderr
