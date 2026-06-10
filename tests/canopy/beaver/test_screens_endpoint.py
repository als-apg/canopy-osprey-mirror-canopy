"""HTTP layer: /beaver/v1/screens (legacy .ui/.py serving)."""

from tests.conftest import READ_HEADERS


def test_list_returns_only_ui_and_py(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    paths = {s["path"] for s in body["screens"]}
    assert paths == {"vacuum.ui", "loader.py", "lcls/ll1/main.ui"}
    assert body["total"] == 3
    sample = body["screens"][0]
    assert "sizeBytes" in sample
    assert "modifiedTsNs" in sample


def test_get_ui_file(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens/vacuum.ui", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/xml"
    assert "<class>Vacuum</class>" in resp.text


def test_get_py_file(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens/loader.py", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/x-python; charset=utf-8"
    assert "pydm loader" in resp.text


def test_get_nested_ui_file(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens/lcls/ll1/main.ui", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/xml"


def test_unknown_screen_404(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens/missing.ui", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "legacy_screen_not_found"


def test_unsupported_suffix_404(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens/readme.md", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "legacy_screen_not_found"


def test_symlink_escape_404(beaver_client, tmp_catalog):
    """A symlink under legacy-screens pointing outside the base must 404.

    URL-encoded ``..`` traversal is handled by HTTP-layer URL normalization
    (httpx and Starlette both collapse ``..`` segments before routing), so the
    real surface for this defense is symlinks — which ``Path.resolve()``
    follows, and which ``_is_within`` catches.
    """
    (tmp_catalog / "secret.ui").write_text("<ui/>", encoding="utf-8")
    link = tmp_catalog / "legacy-screens" / "escape.ui"
    link.symlink_to(tmp_catalog / "secret.ui")
    resp = beaver_client.get("/beaver/v1/screens/escape.ui", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "legacy_screen_not_found"


def test_screens_require_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens")
    assert resp.status_code == 401
    resp = beaver_client.get("/beaver/v1/screens/vacuum.ui")
    assert resp.status_code == 401
