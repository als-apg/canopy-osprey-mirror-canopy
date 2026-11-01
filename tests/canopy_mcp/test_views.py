"""Saved views: the ViewStore on disk and the /views HTTP routes."""

import os
import json
import errno
from pathlib import Path
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.views import (
    MAX_BODY_BYTES,
    ViewStore,
    InvalidView,
    ViewNotFound,
    InvalidViewId,
)
from canopy_mcp.config import Settings

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
JSON = {"content-type": "application/json"}
BAD_IDS = ["..", "a/b", "x.screen", "a" * 65, "", "a b", "é", ".hidden"]


# --- store ----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ViewStore:
    return ViewStore(tmp_path / "drafts")


async def test_round_trip(store: ViewStore) -> None:
    view, created = await store.put("bts-orbit", "  BTS orbit  ", "pv=A:B&range=-1h")
    assert created is True
    assert view["id"] == "bts-orbit"
    assert view["name"] == "BTS orbit"
    assert view["query"] == "pv=A:B&range=-1h"
    assert datetime.fromisoformat(view["updated"]).utcoffset().total_seconds() == 0
    assert store.get("bts-orbit") == view
    on_disk = json.loads((store.root / "views" / "bts-orbit.json").read_text())
    assert on_disk == view


async def test_put_reports_created_then_replaced(store: ViewStore) -> None:
    _, created = await store.put("v", "one", "")
    assert created is True
    view, created = await store.put("v", "two", "pv=X")
    assert created is False
    assert store.get("v")["name"] == "two" == view["name"]


async def test_list_sorted_by_casefolded_name_then_id(store: ViewStore) -> None:
    await store.put("c", "beta", "")
    await store.put("b", "Alpha", "")
    await store.put("a", "alpha", "")
    await store.put("d", "ALPHA", "")
    assert [v["id"] for v in store.list()] == ["a", "b", "d", "c"]


def test_list_empty_without_views_dir(store: ViewStore) -> None:
    assert store.list() == []


async def test_list_skips_unreadable_and_invalid_files(store: ViewStore) -> None:
    await store.put("good", "Good", "")
    views = store.root / "views"
    (views / "garbage.json").write_text("{not json")
    (views / "array.json").write_text("[]")
    (views / "wrongid.json").write_text(json.dumps({"id": "other", "name": "x", "query": "", "updated": "t"}))
    (views / "badname.json").write_text(json.dumps({"id": "badname", "name": "", "query": "", "updated": "t"}))
    (views / "bad.id.json").write_text(json.dumps({"id": "bad.id", "name": "x", "query": "", "updated": "t"}))
    (views / "dir.json").mkdir()
    assert [v["id"] for v in store.list()] == ["good"]
    with pytest.raises(ViewNotFound):
        store.get("garbage")


@pytest.mark.parametrize("bad", BAD_IDS)
async def test_invalid_ids_refused(store: ViewStore, bad: str) -> None:
    with pytest.raises(InvalidViewId):
        store.get(bad)
    with pytest.raises(InvalidViewId):
        await store.put(bad, "n", "")
    with pytest.raises(InvalidViewId):
        await store.delete(bad)
    assert issubclass(InvalidViewId, ValueError)
    assert not (store.root / "views").exists()


async def test_id_of_64_chars_accepted(store: ViewStore) -> None:
    _, created = await store.put("a" * 64, "n", "")
    assert created


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x" * 121, "a\nb", "tab\there", "nul\x00", "del\x7f", "n\x85el", "\ud800", 3, None],
)
async def test_invalid_names_refused(store: ViewStore, name: object) -> None:
    with pytest.raises(InvalidView):
        await store.put("v", name, "")


async def test_name_limits(store: ViewStore) -> None:
    view, _ = await store.put("v", "  " + "x" * 120 + "  ", "")
    assert view["name"] == "x" * 120
    view, _ = await store.put("w", "Ünïcødé ✓", "")
    assert view["name"] == "Ünïcødé ✓"


async def test_query_limits(store: ViewStore) -> None:
    view, _ = await store.put("v", "n", "q" * 4096)
    assert len(view["query"]) == 4096
    with pytest.raises(InvalidView):
        await store.put("v", "n", "q" * 4097)
    # 2048 two-byte characters fit; one more ASCII byte does not.
    await store.put("v", "n", "é" * 2048)
    with pytest.raises(InvalidView):
        await store.put("v", "n", "é" * 2048 + "x")
    for bad in (None, 5, ["pv=A"], "\ud800"):
        with pytest.raises(InvalidView):
            await store.put("v", "n", bad)


async def test_write_is_atomic_and_leaves_no_temp_files(store: ViewStore) -> None:
    await store.put("v", "n", "")
    await store.put("v", "n2", "pv=A")
    files = sorted(p.name for p in (store.root / "views").iterdir())
    assert files == ["v.json"]


async def test_failed_replace_keeps_old_file(store: ViewStore, monkeypatch: pytest.MonkeyPatch) -> None:
    await store.put("v", "old", "")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("canopy_mcp.store.os.replace", boom)
    with pytest.raises(OSError):
        await store.put("v", "new", "")
    assert store.get("v")["name"] == "old"
    assert [p.name for p in (store.root / "views").iterdir()] == ["v.json"]


async def test_delete(store: ViewStore) -> None:
    await store.put("v", "n", "")
    await store.delete("v")
    assert not (store.root / "views" / "v.json").exists()
    with pytest.raises(ViewNotFound):
        store.get("v")
    with pytest.raises(ViewNotFound):
        await store.delete("v")
    assert issubclass(ViewNotFound, LookupError)


# --- routes ---------------------------------------------------------------------


@pytest.fixture
def drafts(tmp_path: Path) -> Path:
    root = tmp_path / "drafts"
    (root / "screens").mkdir(parents=True)
    (root / "screens" / "main.screen.json").write_text(json.dumps({"metadata": {"title": "Main"}}))
    return root


@pytest.fixture
def client(drafts: Path):
    settings = Settings(drafts_dir=drafts, viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


def _put(client: TestClient, view_id: str, body: object, headers: dict[str, str] | None = None):
    return client.put(f"/views/{view_id}", content=json.dumps(body), headers={**JSON, **(headers or {})})


def test_app_state_holds_the_view_store(client: TestClient, drafts: Path) -> None:
    views = client.app.state.views
    assert isinstance(views, ViewStore)
    assert views.views == drafts / "views"


def test_crud_over_http(client: TestClient) -> None:
    r = client.get("/views")
    assert r.status_code == 200
    assert r.json() == {"views": []}
    assert r.headers["cache-control"] == "no-cache"

    r = _put(client, "orbit", {"name": "Orbit", "query": "pv=A:B&range=-1h"})
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-cache"
    created = r.json()
    assert created["id"] == "orbit" and created["name"] == "Orbit" and created["query"] == "pv=A:B&range=-1h"

    r = client.get("/views/orbit")
    assert r.status_code == 200
    assert r.json() == created
    assert r.headers["cache-control"] == "no-cache"

    r = _put(client, "orbit", {"name": "Orbit 2", "query": ""})
    assert r.status_code == 200
    assert r.json()["name"] == "Orbit 2"
    assert client.get("/views").json()["views"] == [r.json()]

    r = client.delete("/views/orbit")
    assert r.status_code == 204
    assert r.content == b""
    assert r.headers["cache-control"] == "no-cache"
    r = client.get("/views/orbit")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-cache"
    assert r.json()["code"] == "not_found"
    r = client.delete("/views/orbit")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-cache"


def test_query_with_a_plot_config_is_stored_verbatim(client: TestClient, drafts: Path) -> None:
    # Trace's axis and display settings ride in the query as `plot=<JSON>`;
    # the server stores the query as an opaque string and never parses it.
    query = (
        "pv=GTL:BPM1:X&pv=GTL:QF1:I&range=-6h"
        "&plot={%22v%22:1,%22axes%22:[{},{%22name%22:%22I%22,%22logScale%22:true}],"
        "%22traces%22:[{},{%22axis%22:1}]}"
    )
    r = _put(client, "gtl", {"name": "GTL", "query": query})
    assert r.status_code == 201
    assert client.get("/views/gtl").json()["query"] == query
    assert json.loads((drafts / "views" / "gtl.json").read_text())["query"] == query


@pytest.mark.parametrize("path", ["/views/a.b", "/views/x.screen", "/views/" + "a" * 65, "/views/a/b", "/views/"])
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_invalid_id_is_400(client: TestClient, path: str, method: str) -> None:
    body = json.dumps({"name": "n", "query": ""}) if method == "PUT" else None
    r = client.request(method, path, content=body, headers=JSON if body else None)
    assert r.status_code == 400
    assert r.headers["cache-control"] == "no-cache"
    assert r.json()["code"] == "invalid_id"


def test_dot_dot_id_never_escapes(client: TestClient, drafts: Path) -> None:
    r = client.put("/views/%2e%2e", content=json.dumps({"name": "n", "query": ""}), headers=JSON)
    assert r.status_code == 400
    assert not (drafts / "views").exists()
    assert not (drafts / "..json").exists()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '"x"',
        "{}",
        '{"name": "n"}',
        '{"query": ""}',
        '{"name": "n", "query": "", "extra": 1}',
        '{"name": "", "query": ""}',
        '{"name": "a\\u0000b", "query": ""}',
        '{"name": 1, "query": ""}',
        '{"name": "n", "query": null}',
        json.dumps({"name": "n", "query": "q" * 4097}),
    ],
)
def test_invalid_body_is_400(client: TestClient, body: str, drafts: Path) -> None:
    r = client.put("/views/v", content=body, headers=JSON)
    assert r.status_code == 400
    assert r.headers["cache-control"] == "no-cache"
    assert r.json()["code"] in {"invalid_json", "invalid_view"} and r.json()["detail"]
    assert not (drafts / "views" / "v.json").exists()


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_put_without_json_content_type_is_415(client: TestClient, content_type: str | None) -> None:
    headers = {"content-type": content_type} if content_type else {}
    r = client.put("/views/v", content=json.dumps({"name": "n", "query": ""}), headers=headers)
    assert r.status_code == 415
    assert r.headers["cache-control"] == "no-cache"


def test_put_accepts_json_with_charset(client: TestClient) -> None:
    r = client.put(
        "/views/v",
        content=json.dumps({"name": "n", "query": ""}),
        headers={"content-type": "Application/JSON; charset=utf-8"},
    )
    assert r.status_code == 201


def test_oversized_body_is_413(client: TestClient, drafts: Path) -> None:
    body = json.dumps({"name": "n", "query": "q" * MAX_BODY_BYTES})
    r = client.put("/views/v", content=body, headers=JSON)
    assert r.status_code == 413
    assert r.headers["cache-control"] == "no-cache"
    assert not (drafts / "views" / "v.json").exists()


def test_oversized_streamed_body_is_413(client: TestClient) -> None:
    def chunks():
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    r = client.put("/views/v", content=chunks(), headers=JSON)
    assert r.status_code == 413


@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
def test_cross_site_writes_are_403(client: TestClient, drafts: Path, site: str) -> None:
    _put(client, "keep", {"name": "Keep", "query": ""})
    r = _put(client, "v", {"name": "n", "query": ""}, {"sec-fetch-site": site})
    assert r.status_code == 403
    assert r.headers["cache-control"] == "no-cache"
    assert not (drafts / "views" / "v.json").exists()
    r = client.delete("/views/keep", headers={"sec-fetch-site": site})
    assert r.status_code == 403
    assert (drafts / "views" / "keep.json").exists()
    # Reads are not guarded.
    assert client.get("/views/keep", headers={"sec-fetch-site": site}).status_code == 200


def _refuse(code: int):
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    return refuse


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_put_on_an_unwritable_views_dir_is_403_read_only_store(
    client: TestClient, drafts: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _put(client, "keep", {"name": "Keep", "query": ""})
    kept = (drafts / "views" / "keep.json").read_bytes()
    monkeypatch.setattr("canopy_mcp.store.os.replace", _refuse(code))
    for view_id in ("keep", "new"):
        r = _put(client, view_id, {"name": "n", "query": "pv=A"})
        assert r.status_code == 403
        assert r.headers["cache-control"] == "no-cache"
        body = r.json()
        assert body["code"] == "read_only_store"
        assert str(drafts) not in body["detail"]
    assert sorted(p.name for p in (drafts / "views").iterdir()) == ["keep.json"]
    assert (drafts / "views" / "keep.json").read_bytes() == kept


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_delete_on_an_unwritable_views_dir_is_403_read_only_store(
    client: TestClient, drafts: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _put(client, "keep", {"name": "Keep", "query": ""})
    monkeypatch.setattr("canopy_mcp.store.os.unlink", _refuse(code))
    r = client.delete("/views/keep")
    assert r.status_code == 403
    assert r.headers["cache-control"] == "no-cache"
    assert r.json() == {"code": "read_only_store", "detail": "saved views cannot be changed here"}
    assert (drafts / "views" / "keep.json").exists()
    # Reads still answer.
    assert client.get("/views/keep").status_code == 200
    assert [v["id"] for v in client.get("/views").json()["views"]] == ["keep"]


def test_a_missing_view_on_an_unwritable_views_dir_is_still_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No views directory exists yet: the delete answers from the lookup, not from the refusal.
    monkeypatch.setattr("canopy_mcp.store.os.unlink", _refuse(errno.EROFS))
    assert client.delete("/views/absent").status_code == 404


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_a_0555_views_dir_answers_403_and_keeps_serving(client: TestClient, drafts: Path) -> None:
    _put(client, "keep", {"name": "Keep", "query": ""})
    views = drafts / "views"
    views.chmod(0o555)
    try:
        assert _put(client, "new", {"name": "n", "query": ""}).status_code == 403
        assert _put(client, "keep", {"name": "n", "query": ""}).status_code == 403
        assert client.delete("/views/keep").status_code == 403
        assert client.delete("/views/absent").status_code == 404
        assert client.get("/views/keep").json()["name"] == "Keep"
    finally:
        views.chmod(0o755)
    assert [p.name for p in views.iterdir()] == ["keep.json"]


def test_a_cross_site_write_is_refused_before_the_store_is_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("canopy_mcp.store.os.replace", _refuse(errno.EROFS))
    r = _put(client, "v", {"name": "n", "query": ""}, {"sec-fetch-site": "cross-site"})
    assert (r.status_code, r.json()) == (403, {"code": "cross_site", "detail": "cross-site writes are refused"})


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_writes_allowed(client: TestClient, site: str) -> None:
    assert _put(client, "v", {"name": "n", "query": ""}, {"sec-fetch-site": site}).status_code == 201
    assert client.delete("/views/v", headers={"sec-fetch-site": site}).status_code == 204


def test_other_methods_are_405_with_no_cache(client: TestClient) -> None:
    for method, path in (("POST", "/views"), ("PUT", "/views"), ("POST", "/views/v"), ("PATCH", "/views/v")):
        r = client.request(method, path)
        assert r.status_code == 405, (method, path)
        assert r.headers["cache-control"] == "no-cache"
        assert "allow" in r.headers


def test_views_reachable_through_proxy_headers(client: TestClient) -> None:
    headers = {"x-forwarded-prefix": "/panel/canopy"}
    assert _put(client, "v", {"name": "n", "query": ""}, headers).status_code == 201
    assert client.get("/views/v", headers=headers).status_code == 200


def test_views_never_reach_the_drafts_surface(client: TestClient, drafts: Path) -> None:
    assert _put(client, "main", {"name": "Main view", "query": "pv=A"}).status_code == 201
    assert (drafts / "views" / "main.json").is_file()
    manifest = client.get("/drafts/manifest.json").json()
    assert [s["id"] for s in manifest["screens"]] == ["main"]
    assert all(not s["path"].startswith("views/") for s in manifest["screens"])
    assert "Main view" not in json.dumps(manifest)
    for path in ("/drafts/views/main.json", "/drafts/views/", "/drafts/views"):
        assert client.get(path).status_code == 404, path


def test_config_names_the_views_api(client: TestClient) -> None:
    assert client.get("/config.json").json()["views"] == {"path": "views"}
