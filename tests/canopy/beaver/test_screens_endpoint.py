"""HTTP layer: /beaver/v1/screens (legacy .ui/.py serving)."""

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from tests.conftest import READ_HEADERS
from canopy.beaver.app import app as beaver_app
from canopy.beaver.config import BeaverConfig
from canopy.beaver.dependencies import get_widget_service
from canopy.beaver.catalog.loader import FileCatalogLoader
from canopy.beaver.services.widget_service import WidgetService


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
    try:
        link.symlink_to(tmp_catalog / "secret.ui")
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform (Windows without Developer Mode)")
    resp = beaver_client.get("/beaver/v1/screens/escape.ui", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "legacy_screen_not_found"


def test_screens_require_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/screens")
    assert resp.status_code == 401
    resp = beaver_client.get("/beaver/v1/screens/vacuum.ui")
    assert resp.status_code == 401


def test_configured_custom_content_type_is_listed_and_served(tmp_catalog):
    # A lab can serve another format (e.g. Phoebus .bob) by configuring its
    # content type in BeaverConfig — no edit to the screens module needed.
    (tmp_catalog / "legacy-screens" / "panel.bob").write_text("<display/>", encoding="utf-8")
    config = BeaverConfig(
        widgets_dir=tmp_catalog / "widgets",
        icons_dir=tmp_catalog / "icons",
        legacy_screens_dir=tmp_catalog / "legacy-screens",
        content_types={".ui": "application/xml", ".py": "text/x-python", ".bob": "application/xml"},
    )
    service = WidgetService(FileCatalogLoader(config).load(), config)
    beaver_app.dependency_overrides[get_widget_service] = lambda: service
    try:
        with TestClient(parent_app) as client:
            listing = client.get("/beaver/v1/screens", headers=READ_HEADERS).json()
            served = client.get("/beaver/v1/screens/panel.bob", headers=READ_HEADERS)
    finally:
        beaver_app.dependency_overrides.pop(get_widget_service, None)
    assert "panel.bob" in {s["path"] for s in listing["screens"]}
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/xml"
