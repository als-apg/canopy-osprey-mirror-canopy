"""Module selection with the default set: every module, which needs every extra.

The cases that hold on an install without the ``data`` extra live in
``test_module_selection.py``.
"""

import os
import sys
import json
import subprocess

import fakeredis
import fakeredis.aioredis
from fastapi.testclient import TestClient

from canopy.jobs import gateway as jobs_gateway
from canopy.main import app, build_app
from tests.conftest import READ_HEADERS
from canopy.core.config import ALL_MODULES, Settings

ELEVEN = (
    "mole",
    "hare",
    "raccoon",
    "vole",
    "fox",
    "beaver",
    "squirrel",
    "magpie",
    "jobs",
    "converter",
    "analytics",
)


def _mount_paths(application) -> list[str]:
    return [route.path for route in application.routes if type(route).__name__ == "Mount"]


def test_the_module_list_is_the_eleven_in_composition_order():
    assert ALL_MODULES == ELEVEN


def test_the_setting_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("CANOPY_MODULES", raising=False)
    assert Settings().modules == ""


def test_canopy_main_app_mounts_all_eleven():
    assert _mount_paths(app) == [f"/{name}" for name in ELEVEN]
    assert app.state.modules == ELEVEN


def test_an_empty_selection_builds_all_eleven():
    assert _mount_paths(build_app("")) == [f"/{name}" for name in ELEVEN]
    assert _mount_paths(build_app([])) == [f"/{name}" for name in ELEVEN]


def test_canopy_main_app_serves_all_eleven_without_canopy_modules(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CANOPY_MODULES"}
    code = (
        "import json, canopy.main\n"
        "print(json.dumps([r.path for r in canopy.main.app.routes if type(r).__name__ == 'Mount']))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [f"/{name}" for name in ELEVEN]


def test_batch_routes_reach_jobs_when_it_is_selected(monkeypatch):
    server = fakeredis.FakeServer()

    async def fake_get_pool():
        return fakeredis.aioredis.FakeRedis(server=server)

    monkeypatch.setattr(jobs_gateway, "get_pool", fake_get_pool)
    with TestClient(app) as client:
        resp = client.get("/converter/v1/conversions/batch/abc/screens/0", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "converter_batch_result_not_found"


def test_a_second_app_with_fewer_modules_does_not_change_the_first(monkeypatch):
    """The selection belongs to each composed app, though both mount the same sub-app objects."""
    build_app(["hare", "converter"])
    server = fakeredis.FakeServer()

    async def fake_get_pool():
        return fakeredis.aioredis.FakeRedis(server=server)

    monkeypatch.setattr(jobs_gateway, "get_pool", fake_get_pool)
    with TestClient(app) as client:
        resp = client.get("/converter/v1/conversions/batch/abc/screens/0", headers=READ_HEADERS)
    assert resp.status_code == 404
