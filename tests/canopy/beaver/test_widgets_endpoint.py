"""HTTP layer: /beaver/v1/widgets/* endpoints."""

from tests.conftest import READ_HEADERS


def test_list_widgets(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {w["id"] for w in body["widgets"]} == {"pv-label", "pv-button"}
    sample = body["widgets"][0]
    assert "displayName" in sample
    assert "supportsPv" in sample
    assert "paletteIcon" in sample


def test_list_widgets_filtered(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets?category=indicators", headers=READ_HEADERS)
    body = resp.json()
    assert [w["id"] for w in body["widgets"]] == ["pv-label"]


def test_get_widget_by_id(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets/pv-label", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "pv-label"
    assert body["qtMapping"]["class"] == "PyDMLabel"
    assert "propSchema" in body
    assert "inspectorSchema" in body


def test_get_widget_unknown(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets/does-not-exist", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "widget_not_found"


def test_by_qt_class(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets/by-qt-class/PyDMLabel", headers=READ_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["id"] == "pv-label"


def test_by_qt_class_unknown(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets/by-qt-class/PyDMNonexistent", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "qt_class_not_mapped"


def test_categories(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets/categories", headers=READ_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    counts = {c["id"]: c["count"] for c in body["categories"]}
    assert counts["indicators"] == 1
    assert counts["controls"] == 1
    # Empty categories are present with zero counts for palette scaffolding.
    assert counts["plots"] == 0
    assert len(body["categories"]) == 6


def test_widgets_require_auth(beaver_client):
    resp = beaver_client.get("/beaver/v1/widgets")
    assert resp.status_code == 401
