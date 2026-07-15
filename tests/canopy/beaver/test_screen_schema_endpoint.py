"""HTTP layer: /beaver/v1/screen-schema."""

from importlib.resources import files

from tests.conftest import READ_HEADERS


def test_list_versions(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"versions": ["1.0"], "total": 1}


def test_get_schema_byte_identical(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema/1.0", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    expected = files("canopy.beaver.data").joinpath("schemas/screen-ir-1.0.schema.json").read_bytes()
    assert resp.content == expected
    assert resp.json()["properties"]["schemaVersion"]["const"] == "1.0"


def test_get_schema_unknown_version(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema/9.9", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "screen_schema_version_not_found"


def test_get_schema_traversal_rejected(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema/..%2fdata", headers=READ_HEADERS)
    assert resp.status_code == 404


def test_list_requires_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema")
    assert resp.status_code == 401


def test_get_requires_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/screen-schema/1.0")
    assert resp.status_code == 401
