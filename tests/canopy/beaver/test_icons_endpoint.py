"""HTTP layer: /beaver/v1/icons/{filename}."""

from tests.conftest import READ_HEADERS


def test_get_icon_succeeds(beaver_client):
    resp = beaver_client.get("/beaver/v1/icons/test.svg", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert "max-age=" in resp.headers["cache-control"]
    assert resp.text.startswith("<svg")


def test_icon_not_found(beaver_client):
    resp = beaver_client.get("/beaver/v1/icons/nonexistent.svg", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "icon_not_found"


def test_icon_traversal_rejected(beaver_client):
    resp = beaver_client.get("/beaver/v1/icons/..bad.svg", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "icon_not_found"


def test_icon_dotfile_rejected(beaver_client):
    resp = beaver_client.get("/beaver/v1/icons/.env", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "icon_not_found"


def test_icon_requires_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/icons/test.svg")
    assert resp.status_code == 401
