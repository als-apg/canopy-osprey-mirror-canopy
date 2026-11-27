"""How the screen manifest is served: off the event loop, from one cache, and in two views.

``test_manifest_cache`` holds a store's listing to the tree and
``test_listing_cache`` what a store remembers. This module holds what
:mod:`canopy_mcp.manifest` builds around the stores:

* a slow listing holds up no other request, and requests that arrive meanwhile
  share it;
* the HTTP routes and the MCP tools read one listing;
* a served listing has a maximum age, and what the listing cache file could not
  vouch for is read in the background;
* ``?view=tree`` is the same list with less in it;
* a changed manifest is told to whoever listens.
"""

# Fixtures are imported from the modules that define them, and a test names them as arguments.
# ruff: noqa: F811

from __future__ import annotations

import json
import asyncio
import hashlib
import threading
from typing import Any
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp import manifest as manifest_module
from canopy_mcp.app import create_app
from canopy_mcp.store import DraftStore, open_stores, listing_cache_path
from canopy_mcp.config import Settings
from canopy_mcp.manifest import FULL, TREE, Manifests, open_manifests
from canopy_mcp.tools.drafts import list_drafts
from canopy_mcp.tools._common import get_store, get_library_store
from canopy_mcp.tools.collections import reset_readers, list_collections

from .test_listing_cache import roots, _restage, count_walks  # noqa: F401
from .test_manifest_cache import _put, opened, _screen  # noqa: F401

LOOPBACK = "http://127.0.0.1"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "drafts_dir": tmp_path / "drafts",
        "library_dir": tmp_path / "library",
        "pv_mode": "hare",
        "api_key": "k",
        "manifest_max_age_s": 0,
    }
    return Settings(**{**fields, **overrides})


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=LOOPBACK)


class Gate:
    """Holds every listing of a store at its start until ``release()``; counts them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, store: DraftStore):
        self.entered = threading.Event()
        self.go = threading.Event()
        self.count = 0
        real = store._list_tree

        def held() -> None:
            self.count += 1
            self.entered.set()
            assert self.go.wait(10), "the listing was never released"
            real()

        monkeypatch.setattr(store, "_list_tree", held)

    def release(self) -> None:
        self.go.set()


# --- nothing blocks the loop --------------------------------------------------------------


async def test_a_slow_listing_holds_up_no_other_request(tmp_path, roots, monkeypatch):
    settings = _settings(tmp_path)
    app = create_app(settings, startup_hooks=[])
    gate = Gate(monkeypatch, app.state.library)
    async with _client(app) as client:
        slow = asyncio.create_task(client.get("/drafts/manifest.json"))
        assert await asyncio.to_thread(gate.entered.wait, 5)
        # The listing is still running; the loop answers everything else meanwhile.
        for _ in range(3):
            assert (await asyncio.wait_for(client.get("/health"), 2)).status_code == 200
        assert (await asyncio.wait_for(client.get("/drafts/library/screens/top.screen.json"), 2)).status_code == 200
        assert not slow.done()
        gate.release()
        assert (await asyncio.wait_for(slow, 5)).status_code == 200


async def test_requests_that_arrive_during_a_listing_share_it(tmp_path, roots, monkeypatch):
    settings = _settings(tmp_path)
    app = create_app(settings, startup_hooks=[])
    gate = Gate(monkeypatch, app.state.library)
    async with _client(app) as client:
        asks = [asyncio.create_task(client.get("/drafts/manifest.json")) for _ in range(8)]
        assert await asyncio.to_thread(gate.entered.wait, 5)
        await asyncio.sleep(0.05)  # every request has reached the route
        gate.release()
        answers = await asyncio.wait_for(asyncio.gather(*asks), 5)
    assert {r.status_code for r in answers} == {200}
    assert len({r.content for r in answers}) == 1
    assert gate.count == 1


async def test_a_listing_tool_holds_up_no_request_either(tmp_path, roots, monkeypatch):
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(roots[0]))
    monkeypatch.setenv("CANOPY_LIBRARY_DIR", str(roots[1]))
    reset_readers()
    app = create_app(startup_hooks=[])
    gate = Gate(monkeypatch, app.state.library)
    async with _client(app) as client:
        for tool in (list_drafts, list_collections):
            gate.entered.clear()
            gate.go.clear()
            call = asyncio.create_task(tool())
            assert await asyncio.to_thread(gate.entered.wait, 5)
            assert (await asyncio.wait_for(client.get("/health"), 2)).status_code == 200
            assert not call.done()
            gate.release()
            await asyncio.wait_for(call, 5)
    reset_readers()


# --- one cache ----------------------------------------------------------------------------


async def test_the_tools_and_the_routes_share_their_stores(tmp_path, roots, monkeypatch, opened):
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(roots[0]))
    monkeypatch.setenv("CANOPY_LIBRARY_DIR", str(roots[1]))
    app = create_app(startup_hooks=[])
    assert get_store() is app.state.store
    assert get_library_store() is app.state.library
    assert open_manifests(app.state.settings) is app.state.manifests
    async with _client(app) as client:
        served = (await client.get("/drafts/manifest.json")).json()["screens"]
        assert len(opened) == 4
        del opened[:]
        # The route has read every screen; the tool lists them without opening one.
        drafts = (await list_drafts())["drafts"]
        assert opened == []
        assert [d["draftId"] for d in drafts] == [
            "mine",
            "library/ring/magnets/quad",
            "library/ring/overview",
            "library/top",
        ]
        assert [d["rev"] for d in drafts] == [s["rev"] for s in served]
        assert drafts[2]["description"] == "The ring."


async def test_a_tool_first_then_the_route_reads_each_screen_once(tmp_path, roots, monkeypatch, opened):
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(roots[0]))
    monkeypatch.setenv("CANOPY_LIBRARY_DIR", str(roots[1]))
    app = create_app(startup_hooks=[])
    await list_drafts()
    assert len(opened) == 4
    del opened[:]
    async with _client(app) as client:
        assert (await client.get("/drafts/manifest.json")).status_code == 200
    assert opened == []


async def test_a_write_through_a_tool_store_shows_in_the_next_manifest_whatever_its_age(tmp_path, roots):
    app = create_app(_settings(tmp_path, manifest_max_age_s=3600), startup_hooks=[])
    async with _client(app) as client:
        first = await client.get("/drafts/manifest.json")
        await app.state.store.write("added", json.loads(_screen("Added")), None)
        second = await client.get("/drafts/manifest.json", headers={"if-none-match": first.headers["etag"]})
        assert second.status_code == 200
        assert "added" in [s["id"] for s in second.json()["screens"]]
        await app.state.store.delete("added")
        third = await client.get("/drafts/manifest.json", headers={"if-none-match": first.headers["etag"]})
        assert third.status_code == 304


def test_nothing_is_written_into_the_library(tmp_path, roots):
    drafts, library = roots
    before = sorted(p.relative_to(library).as_posix() for p in library.rglob("*"))
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, startup_hooks=[])) as client:
        assert client.get("/drafts/manifest.json").status_code == 200
    assert sorted(p.relative_to(library).as_posix() for p in library.rglob("*")) == before
    # By default the listings are kept beside the drafts, one file per store, where no route serves them.
    assert settings.listing_cache_dir == drafts / ".cache"
    kept = {listing_cache_path(drafts / ".cache", root) for root in (drafts, library)}
    assert set((drafts / ".cache").iterdir()) == kept and len(kept) == 2


# --- a maximum age ------------------------------------------------------------------------


async def test_a_young_listing_is_answered_without_looking_at_the_disk(tmp_path, roots, monkeypatch):
    app = create_app(_settings(tmp_path, manifest_max_age_s=3600), startup_hooks=[])
    walks = count_walks(monkeypatch, app.state.store, app.state.library)
    async with _client(app) as client:
        tag = (await client.get("/drafts/manifest.json")).headers["etag"]
        assert sorted(walks) == ["drafts", "library"]
        for _ in range(5):
            assert (await client.get("/drafts/manifest.json", headers={"if-none-match": tag})).status_code == 304
        assert sorted(walks) == ["drafts", "library"]


async def test_a_change_on_disk_shows_once_the_listing_is_older_than_its_age(tmp_path, roots):
    drafts, library = roots
    app = create_app(_settings(tmp_path, manifest_max_age_s=0.2), startup_hooks=[])
    async with _client(app) as client:
        tag = (await client.get("/drafts/manifest.json")).headers["etag"]
        _put(library, "ring/added.screen.json", _screen("Added"))
        (library / "screens" / "top.screen.json").unlink()
        _put(library, "ring/overview.screen.json", _screen("Ring overview, edited"))
        await asyncio.sleep(0.25)
        changed = await client.get("/drafts/manifest.json", headers={"if-none-match": tag})
        assert changed.status_code == 200
        titles = {s["id"]: s["title"] for s in changed.json()["screens"] if s["category"] == "library"}
        assert titles == {
            "ring/added": "Added",
            "ring/magnets/quad": "Quadrupoles",
            "ring/overview": "Ring overview, edited",
        }


def test_the_age_is_a_setting_and_one_second_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_MANIFEST_MAX_AGE_S")
    assert Settings(drafts_dir=tmp_path).manifest_max_age_s == 1.0
    monkeypatch.setenv("CANOPY_MANIFEST_MAX_AGE_S", "0")
    assert Settings(drafts_dir=tmp_path).manifest_max_age_s == 0.0


async def test_the_server_reads_what_the_cache_file_cannot_vouch_for_in_the_background(tmp_path, roots):
    drafts, library = roots
    settings = _settings(tmp_path, cache_dir=tmp_path / "cache", manifest_max_age_s=3600)
    (tmp_path / "cache").mkdir()
    open_stores(drafts, library, cache_dir=settings.listing_cache_dir)[1].manifest()
    _restage(library, lambda data: data.replace(b"Ring overview", b"Ring 0verview"))
    app = create_app(settings, startup_hooks=[])
    async with _client(app) as client:
        first = await client.get("/drafts/manifest.json")
        for _ in range(100):
            now = await client.get("/drafts/manifest.json")
            if now.headers["etag"] != first.headers["etag"]:
                break
            await asyncio.sleep(0.02)
        titles = {s["id"]: s["title"] for s in now.json()["screens"]}
        assert titles["ring/overview"] == "Ring 0verview"


# --- the tree view ------------------------------------------------------------------------


def test_the_tree_view_is_the_same_list_with_only_what_a_tree_shows(tmp_path, roots):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        full = client.get("/drafts/manifest.json")
        tree = client.get("/drafts/manifest.json?view=tree")
        assert tree.status_code == 200
        assert tree.headers["content-type"] == "application/json"
        assert tree.headers["cache-control"] == "no-cache"
        assert tree.json() == {
            "screens": [
                {"id": "mine", "category": "drafts", "title": "Mine"},
                {"id": "ring/magnets/quad", "category": "library", "title": "Quadrupoles"},
                {"id": "ring/overview", "category": "library", "title": "Ring overview"},
                {"id": "top", "category": "library", "title": "Top"},
            ]
        }
        assert [(s["id"], s["category"], s["title"]) for s in full.json()["screens"]] == [
            (s["id"], s["category"], s["title"]) for s in tree.json()["screens"]
        ]
        # What it leaves out follows from what it keeps.
        for entry in full.json()["screens"]:
            prefix = "" if entry["category"] == "drafts" else f"{entry['category']}/"
            assert entry["kind"] == "ir" and entry["path"] == f"{prefix}screens/{entry['id']}.screen.json"
        assert tree.headers["etag"] == f'"{hashlib.sha256(tree.content).hexdigest()}"' != full.headers["etag"]
        assert len(tree.content) < len(full.content)
        again = client.get("/drafts/manifest.json?view=tree", headers={"if-none-match": tree.headers["etag"]})
        assert again.status_code == 304 and again.headers["etag"] == tree.headers["etag"]
        # ``view=full`` is the default, byte for byte.
        assert client.get("/drafts/manifest.json?view=full").content == full.content


def test_the_tree_view_has_an_etag_that_moves_only_with_what_it_shows(tmp_path, roots):
    _, library = roots
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        full = client.get("/drafts/manifest.json").headers["etag"]
        tree = client.get("/drafts/manifest.json?view=tree").headers["etag"]
        # Another body under the same title: the rev moves, the tree does not.
        _put(library, "top.screen.json", _screen("Top", "Now described."))
        assert client.get("/drafts/manifest.json").headers["etag"] != full
        assert client.get("/drafts/manifest.json?view=tree", headers={"if-none-match": tree}).status_code == 304
        _put(library, "top.screen.json", _screen("Top, renamed"))
        assert client.get("/drafts/manifest.json?view=tree", headers={"if-none-match": tree}).status_code == 200


def test_an_unknown_view_is_refused(tmp_path, roots):
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        r = client.get("/drafts/manifest.json?view=flat")
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_view"


NAVIGATION = [{"label": "Ring", "children": [{"screen": "library/ring/overview"}]}]


def test_both_views_carry_the_librarys_navigation(tmp_path, roots):
    _, library = roots
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        plain_full = client.get("/drafts/manifest.json")
        plain_tree = client.get("/drafts/manifest.json?view=tree")
        (library / "navigation.json").write_text(json.dumps(NAVIGATION), encoding="utf-8")
        full = client.get("/drafts/manifest.json")
        tree = client.get("/drafts/manifest.json?view=tree")
        assert full.json() == {**plain_full.json(), "navigation": NAVIGATION}
        assert tree.json() == {**plain_tree.json(), "navigation": NAVIGATION}
        assert full.headers["etag"] != plain_full.headers["etag"]
        assert tree.headers["etag"] != plain_tree.headers["etag"]
        (library / "navigation.json").unlink()
        assert client.get("/drafts/manifest.json").headers["etag"] == plain_full.headers["etag"]
        assert client.get("/drafts/manifest.json?view=tree").headers["etag"] == plain_tree.headers["etag"]


def test_an_unchanged_navigation_is_not_encoded_again(tmp_path, roots, monkeypatch):
    drafts, library = roots
    (library / "navigation.json").write_text(json.dumps(NAVIGATION), encoding="utf-8")
    manifests = Manifests(*open_stores(drafts, library))
    first = manifests.body()
    monkeypatch.setattr(manifest_module.json, "dumps", lambda *a, **k: pytest.fail("encoded again"))
    assert manifests.body() is first


def test_the_navigation_has_the_listings_maximum_age(tmp_path, roots):
    drafts, library = roots
    manifests = Manifests(*open_stores(drafts, library), max_age=60.0)
    plain = manifests.body()
    (library / "navigation.json").write_text(json.dumps(NAVIGATION), encoding="utf-8")
    assert manifests.body() is plain
    manifests.max_age = 0.0
    assert json.loads(manifests.body()[0])["navigation"] == NAVIGATION


# --- announced ----------------------------------------------------------------------------


async def test_a_changed_manifest_is_told_to_the_listeners_once(tmp_path, roots):
    drafts, library = roots
    manifests = Manifests(*open_stores(drafts, library))
    heard: list[str] = []
    unsubscribe = manifests.subscribe(heard.append)
    _, first = await manifests.serve()
    await manifests.serve(TREE)
    await manifests.serve()
    assert heard == []  # the first manifest is no change, and neither is the same one again
    _put(library, "ring/added.screen.json", _screen("Added"))
    await manifests.serve(TREE)  # another view says nothing: the event names the full manifest
    assert heard == []
    body, second = await manifests.serve()
    assert heard == [second] and second != first
    assert second == hashlib.sha256(body).hexdigest()
    await manifests.serve()
    assert heard == [second]
    (library / "screens" / "ring" / "added.screen.json").unlink()
    await manifests.serve()
    assert heard == [second, first]
    unsubscribe()
    _put(library, "ring/added.screen.json", _screen("Added"))
    await manifests.serve()
    assert heard == [second, first]


async def test_a_changed_navigation_is_told_to_the_listeners(tmp_path, roots):
    drafts, library = roots
    manifests = Manifests(*open_stores(drafts, library))
    heard: list[str] = []
    manifests.subscribe(heard.append)
    _, first = await manifests.serve()
    (library / "navigation.json").write_text(json.dumps(NAVIGATION), encoding="utf-8")
    _, second = await manifests.serve()
    assert heard == [second] and second != first
    (library / "navigation.json").unlink()
    await manifests.serve()
    assert heard == [second, first]


async def test_the_watch_looks_only_while_somebody_wants_to_know(tmp_path, roots, monkeypatch):
    drafts, library = roots
    manifests = Manifests(*open_stores(drafts, library))
    walks = count_walks(monkeypatch, manifests.store, manifests.library)
    heard: list[str] = []
    manifests.subscribe(heard.append)
    wanted = False
    task = asyncio.create_task(manifests.watch(lambda: wanted, interval=0.02))
    try:
        await asyncio.sleep(0.15)
        assert walks == []
        wanted = True
        for _ in range(100):
            if walks:
                break
            await asyncio.sleep(0.02)
        await manifests.serve()  # the manifest the change below is a change from
        _put(library, "ring/added.screen.json", _screen("Added"))
        for _ in range(100):
            if heard:
                break
            await asyncio.sleep(0.02)
        assert len(heard) == 1
        assert heard[0] == hashlib.sha256((manifests.body(FULL))[0]).hexdigest()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
