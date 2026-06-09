"""HTTP layer: /beaver/v1/widgets/{id}/validate."""

from tests.conftest import READ_HEADERS


def test_validate_pass(beaver_client):
    resp = beaver_client.post(
        "/beaver/v1/widgets/pv-label/validate",
        headers=READ_HEADERS,
        json={"props": {"pv": "TEST:PV", "precision": 3}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_validate_fail(beaver_client):
    resp = beaver_client.post(
        "/beaver/v1/widgets/pv-label/validate",
        headers=READ_HEADERS,
        json={"props": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["errors"]) >= 1


def test_validate_unknown_widget(beaver_client):
    resp = beaver_client.post(
        "/beaver/v1/widgets/nope/validate",
        headers=READ_HEADERS,
        json={"props": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "widget_not_found"
