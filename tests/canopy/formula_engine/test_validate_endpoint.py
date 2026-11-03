"""Formula Engine /validate endpoint tests."""

import pytest

from tests.conftest import READ_HEADERS


def test_validate_valid_expression(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/validate",
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


def test_validate_invalid_syntax(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/validate",
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


def test_validate_unsafe_expression(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a}",
            "bindings": {"a": "PV:A"},
        },
        headers=READ_HEADERS,
    )
    # A single {a} is valid — it just returns the value
    assert response.status_code == 200


@pytest.mark.parametrize("ref", ["formula:some_id", "fox:some_id"])
def test_validate_circular_reference(formula_engine_client, ref):
    response = formula_engine_client.post(
        "/formula-engine/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} + {b}",
            "bindings": {"a": ref, "b": "PV:B"},
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any(ref in err for err in body["errors"])


def test_validate_requires_auth(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/validate",
        json={
            "kind": "scalar",
            "expression": "{a} + {b}",
            "bindings": {"a": "PV:A", "b": "PV:B"},
        },
    )
    assert response.status_code == 401
