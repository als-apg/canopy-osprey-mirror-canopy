"""Saved views: the ``kind`` field."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette

from canopy_mcp.views import ViewStore, InvalidView, ViewKindConflict, build_view_routes

JSON = {"content-type": "application/json"}


@pytest.fixture
def store(tmp_path: Path) -> ViewStore:
    return ViewStore(tmp_path / "drafts")


@pytest.fixture
def client(store: ViewStore) -> TestClient:
    return TestClient(Starlette(routes=build_view_routes(store)))


def put(client: TestClient, view_id: str, **body):
    return client.put(f"/views/{view_id}", json=body, headers=JSON)


async def test_round_trip_with_kind(store: ViewStore) -> None:
    view, created = await store.put("corr_a", "A", "x=1", "correlation")
    assert created and view["kind"] == "correlation"
    assert store.get("corr_a")["kind"] == "correlation"
    assert json.loads((store.views / "corr_a.json").read_text())["kind"] == "correlation"


async def test_kind_defaults_to_trace(store: ViewStore) -> None:
    view, _ = await store.put("a", "A", "")
    assert view["kind"] == "trace"


def test_legacy_rows_read_as_trace(store: ViewStore) -> None:
    store.views.mkdir(parents=True)
    (store.views / "old.json").write_text(json.dumps({"id": "old", "name": "Old", "query": "", "updated": "t"}))
    assert store.get("old")["kind"] == "trace"
    assert [v["kind"] for v in store.list()] == ["trace"]


async def test_kind_change_raises_conflict(store: ViewStore) -> None:
    await store.put("a", "A", "", "correlation")
    with pytest.raises(ViewKindConflict):
        await store.put("a", "A", "", "trace")
    assert store.get("a")["kind"] == "correlation"


async def test_kindless_put_onto_correlation_conflicts(store: ViewStore) -> None:
    await store.put("a", "A", "", "correlation")
    with pytest.raises(ViewKindConflict):
        await store.put("a", "A", "")


async def test_same_kind_replaces(store: ViewStore) -> None:
    await store.put("a", "A", "", "correlation")
    _, created = await store.put("a", "B", "q", "correlation")
    assert created is False and store.get("a")["name"] == "B"


@pytest.mark.parametrize("kind", ["Trace", "1a", "", "a_b", "a" * 33, 5, "-x"])
async def test_invalid_kind_rejected(store: ViewStore, kind) -> None:
    with pytest.raises(InvalidView):
        await store.put("a", "A", "", kind)


def test_stored_foo_kind_is_listed(store: ViewStore) -> None:
    store.views.mkdir(parents=True)
    (store.views / "f.json").write_text(
        json.dumps({"id": "f", "name": "F", "query": "", "kind": "foo", "updated": "t"})
    )
    assert [(v["id"], v["kind"]) for v in store.list()] == [("f", "foo")]


def test_malformed_stored_kind_is_not_listed(store: ViewStore) -> None:
    store.views.mkdir(parents=True)
    (store.views / "f.json").write_text(
        json.dumps({"id": "f", "name": "F", "query": "", "kind": "Bad Kind", "updated": "t"})
    )
    assert store.list() == []


def test_http_kind_round_trip_and_409(client: TestClient) -> None:
    r = put(client, "corr_a", name="A", query="x=1", kind="correlation")
    assert r.status_code == 201 and r.json()["kind"] == "correlation"
    assert client.get("/views/corr_a").json()["kind"] == "correlation"
    assert client.get("/views").json()["views"][0]["kind"] == "correlation"
    assert put(client, "corr_a", name="A", query="x=1", kind="trace").status_code == 409
    assert put(client, "corr_a", name="A", query="x=1").status_code == 409
    assert put(client, "corr_a", name="A2", query="x=1", kind="correlation").status_code == 200


def test_http_default_kind_and_bad_kind(client: TestClient) -> None:
    assert put(client, "a", name="A", query="").json()["kind"] == "trace"
    assert put(client, "b", name="B", query="", kind="Nope").status_code == 400
    assert put(client, "c", name="C", query="", kind=None).status_code in (200, 201)
    assert put(client, "d", name="D", query="", extra=1).status_code == 400
