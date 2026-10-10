"""The composed app's aggregate ``/docs`` over the selected modules, and the cases that need ``jobs``.

``build_app`` turns off FastAPI's own ``/docs`` and serves one Swagger page whose
spec list is the parent app plus each *selected* module, in composition order.
``jobs`` needs the ``data`` extra, so the closed-meta case that selects it lives
here rather than in ``test_closed_meta.py`` (a pins file); so does the check that
a ``SIM:NEVER:`` write draws the simulator's failure RNG like any other write.
"""

import json

import pytest
from fastapi.testclient import TestClient

from canopy.core import security
from canopy.main import build_app
from canopy.epics.base import PVWriteFailed
from canopy.epics.backends.simulation import SimulationConnector

FIVE = ("hare", "vole", "fox", "mole", "converter")


def _spec_urls(response) -> list[dict[str, str]]:
    line = next(line.strip() for line in response.text.splitlines() if line.strip().startswith("urls: "))
    return json.loads(line.removeprefix("urls: ").rstrip(","))


def test_docs_lists_the_parent_and_the_five_selected_modules_in_composition_order():
    response = TestClient(build_app(FIVE, closed_meta=False)).get("/docs")

    assert response.status_code == 200
    assert _spec_urls(response) == [
        {"url": "/openapi.json", "name": "Canopy"},
        {"url": "/mole/openapi.json", "name": "Mole"},
        {"url": "/hare/openapi.json", "name": "Hare"},
        {"url": "/vole/openapi.json", "name": "Vole"},
        {"url": "/fox/openapi.json", "name": "Fox"},
        {"url": "/converter/openapi.json", "name": "Converter"},
    ]


def test_every_listed_spec_is_served():
    client = TestClient(build_app(FIVE, closed_meta=False))
    for spec in _spec_urls(client.get("/docs")):
        assert client.get(spec["url"]).status_code == 200, spec["url"]


def test_fastapis_own_docs_pages_are_off():
    client = TestClient(build_app(FIVE, closed_meta=False))
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs/oauth2-redirect").status_code == 404


def test_the_parent_app_and_each_selected_sub_app_are_tagged():
    from starlette.routing import Mount

    from canopy.core.modules import module_for_app

    app = build_app(FIVE, closed_meta=False)
    assert module_for_app(app) == "core"
    mounts = {route.path.strip("/"): route.app for route in app.routes if isinstance(route, Mount)}
    assert set(mounts) == set(FIVE)
    for name, sub_app in mounts.items():
        assert module_for_app(sub_app) == name


# --- Closed meta with jobs selected ------------------------------------------------


@pytest.fixture
def hare_key():
    previous = security.get_registry()
    security.bind_key_registry(security.EnvKeyRegistry("hare-key:read@hare"))
    try:
        yield {"X-API-Key": "hare-key"}
    finally:
        security.bind_key_registry(previous)


@pytest.fixture(scope="module")
def closed_with_jobs():
    return build_app(("hare", "fox", "jobs"), closed_meta=True)


def test_a_key_scoped_elsewhere_is_admitted_to_jobs(closed_with_jobs, hare_key):
    """``jobs`` is always allowed: a job's status poll carries nothing the enqueuing module did not return."""
    client = TestClient(closed_with_jobs)
    assert client.get("/jobs/docs", headers=hare_key).status_code == 200
    assert client.get("/jobs/openapi.json", headers=hare_key).status_code == 200


def test_the_same_key_is_refused_by_a_module_it_is_not_scoped_to(closed_with_jobs, hare_key):
    resp = TestClient(closed_with_jobs).get("/fox/docs", headers=hare_key)
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_module_access"


def test_jobs_is_refused_without_a_key(closed_with_jobs):
    assert TestClient(closed_with_jobs).get("/jobs/docs").status_code == 401


# --- The simulator's failure RNG ---------------------------------------------------


async def _outcomes(conn: SimulationConnector, names: list[str]) -> list[bool]:
    outcomes = []
    for name in names:
        try:
            await conn.put(name, 1.0)
        except PVWriteFailed:
            outcomes.append(False)
        else:
            outcomes.append(True)
    return outcomes


async def test_a_never_connecting_write_draws_the_rng_like_any_other_write():
    """The never-connect refusal comes after the draw, so the seeded outcomes of later writes do not move."""
    plain = [f"SIM:P{i}" for i in range(20)]
    with_never = [*plain[:5], "SIM:NEVER:X", *plain[6:]]
    with_other = [*plain[:5], "SIM:OTHER", *plain[6:]]

    never = await _outcomes(SimulationConnector(put_error_rate=0.5, seed=7), with_never)
    other = await _outcomes(SimulationConnector(put_error_rate=0.5, seed=7), with_other)

    assert never[5] is False
    assert never[:5] + never[6:] == other[:5] + other[6:]


async def test_a_never_connecting_write_fails_even_at_a_zero_error_rate():
    conn = SimulationConnector(put_error_rate=0.0, seed=7)
    with pytest.raises(PVWriteFailed, match="never connects"):
        await conn.put("ca://SIM:NEVER:X", 1.0)
    reading = await conn.get("pva://SIM:NEVER:X")
    assert reading.connected is False
    assert reading.value is None
