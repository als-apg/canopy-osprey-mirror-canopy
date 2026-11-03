"""Formula Engine /evaluate endpoint tests."""

import pytest

from tests.conftest import READ_HEADERS


def test_evaluate_scalar(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "scalar",
                "expression": "{a} + {b}",
                "bindings": {"a": "PV:A", "b": "PV:B"},
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "scalar"
    assert body["scalar"]["value"] == 8.0  # 5.0 + 3.0
    assert body["scalar"]["severity"] == "MINOR"  # worst of NO_ALARM and MINOR


def test_evaluate_timeseries(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "timeseries",
                "expression": "{a} + {b}",
                "bindings": {"a": "PV:A", "b": "PV:B"},
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "timeseries"
    timeseries = body["timeseries"]
    assert len(timeseries["tsNs"]) == 10
    assert len(timeseries["values"]) == 10
    # First values: (0+1) + (0+2) = 3.0 (FakeDataSource: arange(10)+1 and arange(10)+2)
    assert timeseries["values"][0] == 3.0
    assert timeseries["decimated"] is False


def test_evaluate_timeseries_binned(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "timeseries",
                "mode": "binned",
                "expression": "{a} + {b}",
                "bindings": {"a": "PV:A", "b": "PV:B"},
                "maxPoints": 5,
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "timeseries"
    timeseries = body["timeseries"]
    assert len(timeseries["tsNs"]) == 5
    assert len(timeseries["values"]) == 5
    assert timeseries["values"][0] == 3.0


def test_evaluate_timeseries_relative_t0(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "timeseries",
                "expression": "{a} + {b}",
                "bindings": {"a": "PV:A", "b": "PV:B"},
            },
            "t0Ns": -5_000_000_000,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "timeseries"
    timeseries = body["timeseries"]
    assert timeseries["tsNs"][0] == 5_000_000_000
    assert timeseries["tsNs"][-1] == 10_000_000_000


def test_evaluate_requires_auth(formula_engine_client):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "scalar",
                "expression": "{a} + {b}",
                "bindings": {"a": "PV:A", "b": "PV:B"},
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
    )
    assert response.status_code == 401


# ``fox:`` is the retired prefix: a stale reference must fail the same clear
# way, not reach the data source as a literal channel name.
@pytest.mark.parametrize("ref", ["formula:derived", "fox:derived"])
def test_evaluate_circular_ref_rejected(formula_engine_client, ref):
    response = formula_engine_client.post(
        "/formula-engine/v1/evaluate",
        json={
            "spec": {
                "kind": "scalar",
                "expression": "{a} + {b}",
                "bindings": {"a": ref, "b": "PV:B"},
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "formula_circular_reference"
