"""Fox /evaluate endpoint tests."""

from tests.conftest import READ_HEADERS


def test_evaluate_scalar(fox_client):
    response = fox_client.post(
        "/fox/v1/evaluate",
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


def test_evaluate_timeseries(fox_client):
    response = fox_client.post(
        "/fox/v1/evaluate",
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


def test_evaluate_requires_auth(fox_client):
    response = fox_client.post(
        "/fox/v1/evaluate",
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


def test_evaluate_circular_ref_rejected(fox_client):
    response = fox_client.post(
        "/fox/v1/evaluate",
        json={
            "spec": {
                "kind": "scalar",
                "expression": "{a} + {b}",
                "bindings": {"a": "fox:derived", "b": "PV:B"},
            },
            "t0Ns": 0,
            "t1Ns": 10_000_000_000,
        },
        headers=READ_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "formula_circular_reference"
