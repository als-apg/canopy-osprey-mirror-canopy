"""The closed-meta gate with the default set: every module, which needs every extra.

The cases that hold on an install without the ``data`` extra live in
``test_closed_meta.py``.
"""

import pytest
from starlette.testclient import WebSocketDenialResponse
from fastapi.testclient import TestClient

from canopy.main import app, build_app
from tests.conftest import READ_HEADERS
from tests.canopy.test_closed_meta import _walk
from canopy.core.config import ALL_MODULES

OPEN = ("GET", "/health")


@pytest.fixture(scope="module")
def closed_app():
    return build_app("", closed_meta=True)


@pytest.fixture(scope="module")
def routes(closed_app):
    return list(_walk(closed_app))


def test_the_walk_reaches_every_module(routes):
    paths = {path for _, _, path in routes}
    for name in ALL_MODULES:
        assert f"/{name}/openapi.json" in paths


def test_every_http_route_and_method_of_all_eleven_is_refused_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    answered = {}
    for kind, method, path in routes:
        if kind == "http" and (method, path) != OPEN:
            status = client.request(method, path).status_code
            if status != 401:
                answered[f"{method} {path}"] = status
    assert answered == {}


def test_options_is_refused_on_every_path_of_all_eleven_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    answered = {}
    for path in sorted({path for _, _, path in routes}):
        status = client.options(path).status_code
        if status != 401:
            answered[path] = status
    assert answered == {}


def test_every_websocket_route_of_all_eleven_is_refused_without_a_key(closed_app, routes):
    client = TestClient(closed_app)
    sockets = [path for kind, _, path in routes if kind == "websocket"]
    assert sockets
    for path in sockets:
        with pytest.raises(WebSocketDenialResponse) as excinfo:
            with client.websocket_connect(path):
                pass
        assert excinfo.value.status_code == 401, path


def test_get_health_answers_without_a_key(closed_app):
    assert TestClient(closed_app).get("/health").status_code == 200


def test_a_registry_key_opens_the_meta_routes(closed_app):
    client = TestClient(closed_app)
    assert client.get("/", headers=READ_HEADERS).status_code == 200
    assert client.get("/analytics/openapi.json", headers=READ_HEADERS).status_code == 200


def test_canopy_main_app_is_open_by_default():
    """No ``CANOPY_CLOSED_META`` in the test environment: the module-level app keeps today's behaviour."""
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/raccoon/openapi.json").status_code == 200
    assert (
        client.options(
            "/", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET"}
        ).status_code
        == 200
    )
