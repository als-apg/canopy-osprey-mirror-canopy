"""The composed app's aggregate ``/docs`` over the selected modules.

``build_app`` turns off FastAPI's own ``/docs`` and serves one Swagger page whose
spec list is the parent app plus each *selected* module, in composition order.
"""

import json

from fastapi.testclient import TestClient

from canopy.main import build_app

FIVE = ("hare", "vole", "formula-engine", "mole", "converter")


def _spec_urls(response) -> list[dict[str, str]]:
    line = next(line.strip() for line in response.text.splitlines() if line.strip().startswith("urls: "))
    return json.loads(line.removeprefix("urls: ").rstrip(","))


def test_docs_lists_the_parent_and_the_five_selected_modules_in_composition_order():
    response = TestClient(build_app(FIVE)).get("/docs")

    assert response.status_code == 200
    assert _spec_urls(response) == [
        {"url": "/openapi.json", "name": "Canopy"},
        {"url": "/mole/openapi.json", "name": "Mole"},
        {"url": "/hare/openapi.json", "name": "Hare"},
        {"url": "/vole/openapi.json", "name": "Vole"},
        {"url": "/formula-engine/openapi.json", "name": "Formula Engine"},
        {"url": "/converter/openapi.json", "name": "Converter"},
    ]


def test_every_listed_spec_is_served():
    client = TestClient(build_app(FIVE))
    for spec in _spec_urls(client.get("/docs")):
        assert client.get(spec["url"]).status_code == 200, spec["url"]


def test_fastapis_own_docs_pages_are_off():
    client = TestClient(build_app(FIVE))
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs/oauth2-redirect").status_code == 404


def test_the_parent_app_and_each_selected_sub_app_are_tagged():
    from starlette.routing import Mount

    from canopy.core.modules import module_for_app

    app = build_app(FIVE)
    assert module_for_app(app) == "core"
    mounts = {route.path.strip("/"): route.app for route in app.routes if isinstance(route, Mount)}
    assert set(mounts) == set(FIVE)
    for name, sub_app in mounts.items():
        assert module_for_app(sub_app) == name
