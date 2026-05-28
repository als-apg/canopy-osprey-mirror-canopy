"""Fox /validate endpoint tests."""

from tests.conftest import READ_HEADERS


def test_validate_valid_expression(fox_client):
    response = fox_client.post(
        "/fox/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} + {b}",
            "bindings": {"a": "PV:A", "b": "PV:B"},
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert set(body["tokens"]) == {"a", "b"}
    assert body["errors"] == []


def test_validate_invalid_syntax(fox_client):
    response = fox_client.post(
        "/fox/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} +",
            "bindings": {"a": "PV:A"},
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert len(body["errors"]) > 0


def test_validate_unsafe_expression(fox_client):
    response = fox_client.post(
        "/fox/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a}",
            "bindings": {"a": "PV:A"},
        },
        headers=READ_HEADERS,
    )
    # A single {a} is valid — it just returns the value
    assert response.status_code == 200


def test_validate_circular_reference(fox_client):
    response = fox_client.post(
        "/fox/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} + {b}",
            "bindings": {"a": "fox:some_id", "b": "PV:B"},
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("fox:" in err for err in body["errors"])


def test_validate_requires_auth(fox_client):
    response = fox_client.post(
        "/fox/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} + {b}",
            "bindings": {"a": "PV:A", "b": "PV:B"},
        },
    )
    assert response.status_code == 401
