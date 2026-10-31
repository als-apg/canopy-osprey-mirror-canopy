"""``CANOPY_VIEWS_DIR``: the directory the saved-view files are written to and read from."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.views import ViewStore
from canopy_mcp.config import Settings

JSON = {"Content-Type": "application/json"}


def _put(client: TestClient, view_id: str, name: str = "Orbit"):
    return client.put(f"/views/{view_id}", content=json.dumps({"name": name, "query": "pv=A:B"}), headers=JSON)


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_views_dir_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_VIEWS_DIR", raising=False)
    assert Settings().views_dir is None


def test_views_dir_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CANOPY_VIEWS_DIR", str(tmp_path / "agent-data" / "views"))
    assert Settings().views_dir == tmp_path / "agent-data" / "views"


def test_an_empty_views_dir_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_VIEWS_DIR", "")
    assert Settings().views_dir is None


def test_store_without_a_views_dir_uses_views_under_the_root(tmp_path: Path) -> None:
    store = ViewStore(tmp_path / "drafts")
    assert store.views == tmp_path / "drafts" / "views"


async def test_store_writes_the_json_files_directly_into_the_named_dir(tmp_path: Path) -> None:
    named = tmp_path / "volume" / "saved"
    store = ViewStore(tmp_path / "drafts", views_dir=named)
    await store.put("orbit", "Orbit", "pv=A:B")
    assert store.path_for("orbit") == named / "orbit.json"
    assert _files(tmp_path) == ["volume/saved/orbit.json"]
    assert [view["id"] for view in store.list()] == ["orbit"]
    await store.delete("orbit")
    assert _files(tmp_path) == []


def test_views_land_in_the_named_dir(tmp_path: Path) -> None:
    drafts, named = tmp_path / "drafts", tmp_path / "volume" / "saved"
    app = create_app(Settings(drafts_dir=drafts, views_dir=named), startup_hooks=[])
    with TestClient(app) as client:
        assert app.state.views.views == named
        assert _put(client, "orbit").status_code == 201
        assert json.loads((named / "orbit.json").read_text())["name"] == "Orbit"
        assert not (drafts / "views").exists()
        assert not (named / "views").exists()
        assert [view["id"] for view in client.get("/views").json()["views"]] == ["orbit"]
        assert client.get("/views/orbit").status_code == 200
        assert client.delete("/views/orbit").status_code == 204
    assert _files(named) == []


def test_views_already_in_the_named_dir_are_listed(tmp_path: Path) -> None:
    named = tmp_path / "saved"
    named.mkdir()
    view = {"id": "kept", "name": "Kept", "query": "", "kind": "trace", "updated": "2026-01-01T00:00:00.000Z"}
    (named / "kept.json").write_text(json.dumps(view))
    app = create_app(Settings(drafts_dir=tmp_path / "drafts", views_dir=named), startup_hooks=[])
    with TestClient(app) as client:
        assert client.get("/views").json() == {"views": [view]}


def test_without_the_variable_views_land_under_the_drafts_dir(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    app = create_app(Settings(drafts_dir=drafts), startup_hooks=[])
    with TestClient(app) as client:
        assert _put(client, "orbit").status_code == 201
    assert _files(tmp_path) == ["drafts/views/orbit.json"]


def test_two_users_with_their_own_views_dir_do_not_see_each_other(tmp_path: Path) -> None:
    """One shared drafts dir, one views dir per user: saved views stay per user."""
    drafts = tmp_path / "drafts"
    apps = {
        user: create_app(Settings(drafts_dir=drafts, views_dir=tmp_path / user), startup_hooks=[])
        for user in ("alice", "bob")
    }
    with TestClient(apps["alice"]) as alice, TestClient(apps["bob"]) as bob:
        assert _put(alice, "mine", "Alice's").status_code == 201
        assert bob.get("/views").json() == {"views": []}
        assert bob.get("/views/mine").status_code == 404
