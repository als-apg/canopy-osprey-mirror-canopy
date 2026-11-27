"""What the event stream says of the server: a changed manifest, a written collection, and that it does.

With the workspace on, the active window's stream carries ``manifest`` and
``collections`` events beside the slot protocol, and its ``hello`` names them in
``capabilities`` so a viewer knows it need not ask at an interval.
"""

# Fixtures are imported from the modules that define them, and a test names them as arguments.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
from typing import Any
from pathlib import Path

import pytest
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

import canopy_mcp.manifest as manifest_module
import canopy_mcp.workspace as hubs
from canopy_mcp.app import create_app
from canopy_mcp.shared_library import CAPABILITIES

from .test_listing_cache import roots, count_walks  # noqa: F401
from .test_workspace_hub import _state
from .test_manifest_cache import _put, _screen
from .test_manifest_serving import _client, _settings
from .test_workspace_routes import Exchange, _url


def _workspace_app(tmp_path: Path, **overrides: Any):
    return create_app(_settings(tmp_path, workspace=True, **overrides), startup_hooks=[])


def _state_sample() -> Any:
    return _state("$sample")


async def _open_stream(app) -> Exchange:
    hubs.get_hub().claim("w-A", 41, True, _state_sample())
    stream = Exchange(app, "GET", _url("events?window=w-A"), None, None)
    await stream.until(want_events=1)
    return stream


async def _wait_for_events(stream: Exchange, count: int) -> list[dict[str, Any]]:
    for _ in range(150):
        if len(stream.events) >= count:
            break
        await asyncio.sleep(0.02)
    return stream.events


@pytest.fixture
def fresh_hub():
    hubs.reset_hub()
    AppStatus.should_exit = False
    yield
    hubs.reset_hub()
    AppStatus.should_exit = False


async def test_hello_names_what_the_stream_carries(tmp_path, roots, fresh_hub):
    app = _workspace_app(tmp_path)
    stream = await _open_stream(app)
    try:
        hello = stream.events[0]
        assert hello["event"] == "hello"
        assert hello["data"]["capabilities"] == ["manifest-events", "collections-events", "manifest-tree-view"]
        assert tuple(hello["data"]["capabilities"]) == CAPABILITIES
        # What protocol 1 put there is still there.
        assert {"epoch", "activeWindow", "seq"} <= set(hello["data"])
        assert hello["data"]["activeWindow"] == "w-A" and hello["data"]["seq"] == 41
    finally:
        await stream.close()


async def test_a_changed_manifest_reaches_the_stream_with_its_etag(tmp_path, roots, fresh_hub):
    _, library = roots
    app = _workspace_app(tmp_path)
    async with app.router.lifespan_context(app), _client(app) as client:
        first = await client.get("/drafts/manifest.json")
        stream = await _open_stream(app)
        try:
            _put(library, "ring/added.screen.json", _screen("Added"))
            changed = await client.get("/drafts/manifest.json", headers={"if-none-match": first.headers["etag"]})
            assert changed.status_code == 200
            events = await _wait_for_events(stream, 2)
            assert [e["event"] for e in events] == ["hello", "manifest"]
            assert events[1]["data"] == {"etag": changed.headers["etag"]}
            # The etag is the header itself: handed back as If-None-Match it is a 304.
            again = await client.get("/drafts/manifest.json", headers={"if-none-match": events[1]["data"]["etag"]})
            assert again.status_code == 304
        finally:
            await stream.close()


async def test_a_change_on_disk_reaches_the_stream_with_nobody_asking(tmp_path, roots, fresh_hub, monkeypatch):
    monkeypatch.setattr(manifest_module, "WATCH_S", 0.05)
    _, library = roots
    app = _workspace_app(tmp_path)
    walks = count_walks(monkeypatch, app.state.library)
    async with app.router.lifespan_context(app), _client(app) as client:
        first = await client.get("/drafts/manifest.json")
        del walks[:]
        await asyncio.sleep(0.2)
        assert walks == []  # no stream is open: the disk is left alone
        stream = await _open_stream(app)
        try:
            (library / "screens" / "top.screen.json").unlink()
            events = await _wait_for_events(stream, 2)
            assert [e["event"] for e in events] == ["hello", "manifest"]
            now = await client.get("/drafts/manifest.json")
            assert events[1]["data"] == {"etag": now.headers["etag"]} != {"etag": first.headers["etag"]}
            assert "top" not in [s["id"] for s in now.json()["screens"]]
        finally:
            await stream.close()


async def test_a_collection_write_reaches_the_stream(tmp_path, roots, fresh_hub):
    app = _workspace_app(tmp_path)
    doc = {"name": "Mine", "members": [{"screen": "library/top"}]}
    async with _client(app) as client:
        stream = await _open_stream(app)
        try:
            headers = {"if-none-match": "*"}
            assert (await client.put("/collections/mine", json=doc, headers=headers)).status_code == 201
            assert [e["event"] for e in await _wait_for_events(stream, 2)] == ["hello", "collections"]
            assert stream.events[1]["data"] == {}
            # A refused write and a read say nothing.
            assert (await client.put("/collections/mine", json=doc, headers=headers)).status_code == 412
            assert (await client.get("/collections")).status_code == 200
            assert (await client.get("/collections-state")).status_code == 200
            op = {"op": "untick", "ref": "personal:mine"}
            posted = await client.post("/collections-state", json=op)
            assert posted.status_code == 200, posted.text
            assert (await client.delete("/collections/mine")).status_code == 204
            events = await _wait_for_events(stream, 4)
            assert [e["event"] for e in events] == ["hello", "collections", "collections", "collections"]
        finally:
            await stream.close()


def test_the_collections_routes_work_without_a_workspace(tmp_path, roots):
    doc = {"name": "Mine", "members": [{"screen": "library/top"}]}
    with TestClient(create_app(_settings(tmp_path), startup_hooks=[])) as client:
        assert client.put("/collections/mine", json=doc, headers={"if-none-match": "*"}).status_code == 201
        assert client.delete("/collections/mine").status_code == 204


def test_a_broadcast_reaches_every_open_stream_and_leaves_the_slot_alone():
    hub = hubs.Hub(epoch="e1")
    assert not hub.has_streams
    hub.broadcast("manifest", {"etag": '"x"'})  # nobody listens: nothing happens
    hub.claim("w-A", 1, True, _state_sample())
    stream = hub.connect("w-A")
    assert hub.has_streams
    before = hub.document()
    payload = {"etag": '"x"'}
    hub.broadcast("manifest", payload)
    payload["etag"] = "changed afterwards"
    assert stream.drain() == [
        {"event": "hello", "data": {"epoch": "e1", "activeWindow": "w-A", "seq": 1}},
        {"event": "manifest", "data": {"etag": '"x"'}},
    ]
    assert hub.document() == before
    hub.disconnect(stream)
    assert not hub.has_streams
