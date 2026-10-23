"""The read-only library root beside the personal drafts (``CANOPY_LIBRARY_DIR``).

A library screen is ``library/<id>`` to the tools, ``{id, category: "library",
path: "library/screens/<id>.screen.json"}`` in the manifest and
``/embed/library/<id>`` in the viewer. Only its ``screens/`` subtree is served,
no tool can change it, and without the variable nothing here exists.
"""

import json
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.urls import draft_url, embed_path
from canopy_mcp.store import DraftStore, ReadOnlyStore, InvalidDraftId, open_stores
from canopy_mcp.tools import push as push_module
from canopy_mcp.tools import check as check_module
from canopy_mcp.tools import drafts, _common, convert, screenshot
from canopy_mcp.config import Settings
from canopy_mcp.server import AUTHORING_TOOLS
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.tools.push import request_push
from canopy_mcp.tools.check import check
from canopy_mcp.tools.drafts import (
    add_node,
    get_screen,
    new_screen,
    patch_node,
    list_drafts,
    remove_node,
    delete_draft,
)
from canopy_mcp.tools.convert import convert_legacy
from tests.canopy_mcp.synthetic_spec import write_spec

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
PNG = b"\x89PNG\r\n\x1a\nfake"
LIBRARY_ID = "library/sr/overview"


def _doc(title: str, width: int = 640, height: int = 480) -> dict:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": title, "source": {"type": "native"}, "size": {"width": width, "height": height}},
        "macros": [],
        "root": {
            "id": "root",
            "type": "group",
            "props": {},
            "geometry": {"x": 0, "y": 0, "width": width, "height": height},
            "children": [
                {"id": "a", "type": "label", "props": {}, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}
            ],
        },
    }


def _embed(node_id: str, file: str) -> dict:
    geometry = {"x": 0, "y": 0, "width": 1, "height": 1}
    return {"id": node_id, "type": "embedded-display", "props": {"file": file}, "geometry": geometry}


def _unresolved(result: dict) -> list[str]:
    return [f["nodeId"] for f in result["semantic"] if f["code"] == "unresolved_ref"]


def _raising(function):
    async def method(self, *args, **kwargs):
        return function(self, *args, **kwargs)

    return method


def _put(path: Path, content: dict | bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        content = json.dumps(content)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _tree(root: Path) -> list[tuple[str, bytes]]:
    return sorted((p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """A personal root and a library root, the library with files that must never be served."""
    personal, library = tmp_path / "drafts", tmp_path / "library"
    _put(personal / "screens" / "mine" / "view.screen.json", _doc("Mine"))
    # Written before a library was configured: the id is reserved now.
    _put(personal / "screens" / "library" / "old.screen.json", _doc("Shadow"))
    _put(personal / "screens" / "library" / "old.png", PNG)
    _put(library / "screens" / "sr" / "overview.screen.json", _doc("SR overview"))
    _put(library / "screens" / "sr" / "logo.png", PNG)
    _put(library / "extra" / "x.png", PNG)
    _put(library / "top.screen.json", _doc("Outside screens"))
    _put(library / "WRITE-BINDINGS.json", {"writeBindings": []})
    _put(library / "WRITE-BINDINGS.md", "# write bindings\n")
    return personal, library


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    for module in (drafts, convert, check_module, screenshot, push_module, _common):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    drafts.reset_stores()
    ir_map = {t: {"consumes": [], "verbatim": False} for t in ("group", "label", "embedded-display")}
    set_catalog(build_catalog(ir_map))


@pytest.fixture
def settings(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    personal, library = roots
    spec = write_spec(tmp_path / "spec")
    s = Settings(base_url="http://canopy.test", drafts_dir=personal, library_dir=library, spec_dir=spec)
    _patch_settings(monkeypatch, s)
    yield s
    set_catalog(None)
    drafts.reset_stores()


@pytest.fixture
def personal_only(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):
    s = Settings(base_url="http://canopy.test", drafts_dir=roots[0])
    _patch_settings(monkeypatch, s)
    yield s
    set_catalog(None)
    drafts.reset_stores()


@pytest.fixture
def client(roots: tuple[Path, Path]):
    personal, library = roots
    s = Settings(drafts_dir=personal, library_dir=library, viewer_dist=FIXTURE_DIST, pv_mode="mock")
    with TestClient(create_app(s, startup_hooks=[])) as c:
        yield c


# --- settings -----------------------------------------------------------------------


def test_library_dir_is_unset_by_default_and_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CANOPY_LIBRARY_DIR", raising=False)
    assert Settings().library_dir is None
    monkeypatch.setenv("CANOPY_LIBRARY_DIR", "")
    assert Settings().library_dir is None
    assert Settings(library_dir="").library_dir is None
    monkeypatch.setenv("CANOPY_LIBRARY_DIR", str(tmp_path))
    assert Settings().library_dir == tmp_path


# --- the two stores -----------------------------------------------------------------


def test_open_stores_without_a_library_is_one_plain_store(tmp_path: Path) -> None:
    personal, library = open_stores(tmp_path / "drafts", None)

    assert library is None
    assert (personal.read_only, personal.category, personal.reserved) == (False, "drafts", frozenset())
    assert personal.check_id("library/x") == "library/x"


def test_library_store_is_read_only_and_lists_bare_ids(roots: tuple[Path, Path]) -> None:
    _, library = open_stores(*roots)

    assert library is not None and library.read_only
    assert library.manifest()["screens"] == [
        {
            "id": "sr/overview",
            "kind": "ir",
            "category": "library",
            "title": "SR overview",
            "path": "library/screens/sr/overview.screen.json",
            "rev": library.rev("sr/overview"),
        }
    ]
    # The id in the entry is the one the same store reads by.
    assert library.read("sr/overview")[0]["metadata"]["title"] == "SR overview"


@pytest.mark.parametrize("draft_id", ["library", "library/old", "library/a/b"])
def test_personal_store_refuses_the_reserved_first_segment(roots: tuple[Path, Path], draft_id: str) -> None:
    personal, _ = open_stores(*roots)

    with pytest.raises(InvalidDraftId, match="reserved"):
        personal.check_id(draft_id)


def test_personal_store_keeps_ids_that_only_start_with_the_word(roots: tuple[Path, Path]) -> None:
    personal, _ = open_stores(*roots)

    assert personal.check_id("library2/x") == "library2/x"
    assert personal.check_id("mine/library") == "mine/library"


def test_personal_store_neither_lists_nor_serves_reserved_ids(roots: tuple[Path, Path]) -> None:
    personal, _ = open_stores(*roots)

    assert personal.list() == ["mine/view"]
    assert [e["id"] for e in personal.manifest()["screens"]] == ["mine/view"]
    assert personal.resolve_static("screens/library/old.screen.json") is None
    assert personal.resolve_static("screens/library/old.png") is None
    assert personal.resolve_static("screens/mine/view.screen.json") is not None
    # The same tree without a library beside it is served and listed as before.
    assert DraftStore(roots[0]).list() == ["library/old", "mine/view"]
    assert DraftStore(roots[0]).resolve_static("screens/library/old.png") is not None


@pytest.mark.parametrize(
    "relpath",
    [
        "extra/x.png",
        "top.screen.json",
        "WRITE-BINDINGS.json",
        "WRITE-BINDINGS.md",
        "%65xtra/x.png",
        "screens/../extra/x.png",
        "screens/%2e%2e/extra/x.png",
        "screens/%252e%252e/top.screen.json",
        "",
    ],
)
def test_library_store_serves_nothing_outside_screens(roots: tuple[Path, Path], relpath: str) -> None:
    _, library = open_stores(*roots)

    assert library is not None
    assert library.resolve_static(relpath) is None


def test_library_store_serves_screens_and_images_under_screens(roots: tuple[Path, Path]) -> None:
    _, library = open_stores(*roots)

    assert library is not None
    assert library.resolve_static("screens/sr/overview.screen.json") is not None
    assert library.resolve_static("screens/sr/logo.png") is not None
    assert library.resolve_static("%73creens/sr/logo.png") is not None  # decoded before the rule applies


def test_a_library_root_that_does_not_exist_is_an_empty_library(tmp_path: Path) -> None:
    _, library = open_stores(tmp_path / "drafts", tmp_path / "absent")

    assert library is not None
    assert library.manifest() == {"screens": []}
    assert not (tmp_path / "absent").exists()


# --- urls ---------------------------------------------------------------------------


def test_library_ids_open_under_the_library_category(settings: Settings) -> None:
    assert draft_url(settings, LIBRARY_ID) == "http://127.0.0.1:8765/embed/library/sr/overview"
    assert draft_url(settings, "mine/view") == "http://127.0.0.1:8765/embed/drafts/mine/view"
    panel = settings.model_copy(update={"panel_id": "canopy"})
    assert draft_url(panel, LIBRARY_ID) == "/panel/canopy/embed/library/sr/overview"


def test_without_a_library_the_same_id_is_a_draft(personal_only: Settings) -> None:
    assert draft_url(personal_only, "library/sr/overview") == "http://127.0.0.1:8765/embed/drafts/library/sr/overview"


def test_embed_url_takes_the_category() -> None:
    url = screenshot.embed_url("http://viewer.test/", "sr/overview", {"P": "SR"}, category="library")

    assert url == "http://viewer.test/embed/library/sr/overview?_canopyZoom=100&P=SR&_canopyChrome=0"
    assert embed_path("sr/overview") == "/embed/drafts/sr/overview"


# --- http ---------------------------------------------------------------------------


def test_manifest_lists_both_roots(client: TestClient) -> None:
    screens = client.get("/drafts/manifest.json").json()["screens"]

    assert [(s["id"], s["category"], s["path"]) for s in screens] == [
        ("mine/view", "drafts", "screens/mine/view.screen.json"),
        ("sr/overview", "library", "library/screens/sr/overview.screen.json"),
    ]
    # Every listed path is fetchable under /drafts/.
    for entry in screens:
        assert client.get(f"/drafts/{entry['path']}").json()["metadata"]["title"] == entry["title"]


def test_manifest_etag_follows_the_library(client: TestClient, roots: tuple[Path, Path]) -> None:
    before = client.get("/drafts/manifest.json").headers["etag"]
    _put(roots[1] / "screens" / "sr" / "second.screen.json", _doc("Second"))

    assert client.get("/drafts/manifest.json").headers["etag"] != before


def test_library_screen_is_served_with_its_rev_as_etag(client: TestClient, roots: tuple[Path, Path]) -> None:
    r = client.get("/drafts/library/screens/sr/overview.screen.json")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["etag"] == f'"{DraftStore(roots[1]).rev("sr/overview")}"'
    assert "content-security-policy" not in r.headers
    again = client.get(r.request.url.path, headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_library_image_is_sandboxed_like_a_draft_image(client: TestClient) -> None:
    r = client.get("/drafts/library/screens/sr/logo.png")

    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/drafts/library/extra/x.png",
        "/drafts/library/WRITE-BINDINGS.json",
        "/drafts/library/WRITE-BINDINGS.md",
        "/drafts/library/top.screen.json",
        "/drafts/library/%65xtra/x.png",
        "/drafts/library/screens/%2e%2e/extra/x.png",
        "/drafts/library/screens/%252e%252e/top.screen.json",
        "/drafts/library/",
        "/drafts/library",
        # The personal files under the reserved segment.
        "/drafts/screens/library/old.screen.json",
        "/drafts/screens/library/old.png",
    ],
)
def test_nothing_else_is_served_under_the_library_prefix(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_library_prefix_never_reaches_the_personal_root(tmp_path: Path) -> None:
    personal, library = tmp_path / "drafts", tmp_path / "library"
    _put(personal / "library" / "screens" / "p.screen.json", _doc("Personal file under a library/ directory"))
    _put(library / "screens" / "l.screen.json", _doc("Library"))
    s = Settings(drafts_dir=personal, library_dir=library, viewer_dist=FIXTURE_DIST, pv_mode="mock")

    with TestClient(create_app(s, startup_hooks=[])) as c:
        assert c.get("/drafts/library/screens/p.screen.json").status_code == 404
        assert c.get("/drafts/%256Cibrary/screens/p.screen.json").status_code == 404
        assert c.get("/drafts/library/screens/l.screen.json").status_code == 200
        assert c.app.state.library.read_only


def test_without_a_library_the_http_surface_is_unchanged(roots: tuple[Path, Path]) -> None:
    s = Settings(drafts_dir=roots[0], viewer_dist=FIXTURE_DIST, pv_mode="mock")

    with TestClient(create_app(s, startup_hooks=[])) as c:
        screens = c.get("/drafts/manifest.json").json()["screens"]
        assert [(e["id"], e["category"], e["path"]) for e in screens] == [
            ("library/old", "drafts", "screens/library/old.screen.json"),
            ("mine/view", "drafts", "screens/mine/view.screen.json"),
        ]
        assert c.get("/drafts/screens/library/old.screen.json").status_code == 200
        assert c.get("/drafts/library/screens/sr/overview.screen.json").status_code == 404
        assert c.app.state.library is None


# --- tools: reading -----------------------------------------------------------------


async def test_get_screen_reads_a_library_screen(settings: Settings) -> None:
    result = await get_screen(LIBRARY_ID)

    assert result["draftId"] == LIBRARY_ID
    assert result["screen"]["metadata"]["title"] == "SR overview"
    assert result["rev"] == _common.get_library_store(settings).rev("sr/overview")
    assert result["url"] == "http://127.0.0.1:8765/embed/library/sr/overview"


async def test_get_screen_names_the_given_id_when_the_library_has_no_such_screen(settings: Settings) -> None:
    with pytest.raises(ToolError, match="no draft stored under 'library/sr/absent'"):
        await get_screen("library/sr/absent")


async def test_check_accepts_a_library_id(settings: Settings) -> None:
    stored = await check(draft_id=LIBRARY_ID)
    assert not [f for group in ("schema", "semantic", "lint") for f in stored[group] if f["severity"] == "error"]

    # A document checked at a library id resolves its refs in the library, not in the drafts:
    # ``overview`` exists beside ``library/sr/probe`` and ``view`` only beside ``mine/probe``.
    doc = _doc("Probe")
    doc["root"]["children"] += [_embed("lib", "overview.screen.json"), _embed("own", "view.screen.json")]
    at_library = await check(screen=doc, draft_id="library/sr/probe")
    at_drafts = await check(screen=doc, draft_id="mine/probe")
    assert _unresolved(at_library) == ["own"]
    assert _unresolved(at_drafts) == ["lib"]
    with pytest.raises(ToolError, match="no draft stored under 'library/sr/absent'"):
        await check(draft_id="library/sr/absent")


async def test_screenshot_renders_a_library_screen(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def capture(browser, draft_id, macros=None, **kwargs):
        calls.append((draft_id, macros, kwargs))
        return screenshot.PNG_SIGNATURE, {"width": 1, "height": 1, "pv": "disconnected"}

    monkeypatch.setattr(screenshot, "get_browser", lambda: object())
    monkeypatch.setattr(screenshot, "capture", capture)

    await screenshot.screenshot(LIBRARY_ID, {"P": "SR"})
    await screenshot.screenshot("mine/view")

    assert calls == [
        ("sr/overview", {"P": "SR"}, {"size": {"width": 640, "height": 480}, "theme": None, "category": "library"}),
        ("mine/view", None, {"size": {"width": 640, "height": 480}, "theme": None, "category": "drafts"}),
    ]


async def test_list_drafts_lists_both_roots(settings: Settings) -> None:
    listed = (await list_drafts())["drafts"]

    assert [(d["draftId"], d["path"], d["url"]) for d in listed] == [
        ("mine/view", "screens/mine/view.screen.json", "http://127.0.0.1:8765/embed/drafts/mine/view"),
        (
            LIBRARY_ID,
            "library/screens/sr/overview.screen.json",
            "http://127.0.0.1:8765/embed/library/sr/overview",
        ),
    ]
    # Every listed id is one get_screen takes.
    for entry in listed:
        assert (await get_screen(entry["draftId"]))["rev"] == entry["rev"]


async def test_list_drafts_carries_library_descriptions(settings: Settings, roots: tuple[Path, Path]) -> None:
    doc = _doc("SR overview")
    doc["metadata"]["description"] = "Storage-ring overview. Lists every sector."
    _put(roots[1] / "screens" / "sr" / "overview.screen.json", doc)

    listed = {d["draftId"]: d["description"] for d in (await list_drafts())["drafts"]}

    assert listed[LIBRARY_ID] == "Storage-ring overview."
    assert listed["mine/view"] == ""


# --- tools: writing -----------------------------------------------------------------


async def _call_authoring_tool(name: str, draft_id: str, rev: str) -> dict:
    node = {"id": "n", "type": "label", "props": {}, "geometry": {"x": 0, "y": 0, "width": 1, "height": 1}}
    calls = {
        "new_screen": lambda: new_screen(draft_id, _doc("New"), overwrite=True, base_rev=rev),
        "patch_node": lambda: patch_node(draft_id, "a", rev, {"props": {"text": "x"}}),
        "add_node": lambda: add_node(draft_id, "root", node, rev),
        "remove_node": lambda: remove_node(draft_id, "a", rev),
        "delete_draft": lambda: delete_draft(draft_id, rev),
        # No converter is mocked: a request would fail the test (pytest-httpx refuses it).
        "convert_legacy": lambda: convert_legacy(draft_id, content="<display/>", kind="bob", overwrite=True),
        "request_push": lambda: request_push(draft_id, "sr", "note"),
    }
    return await calls[name]()


def test_the_seven_authoring_tools_are_the_ones_covered() -> None:
    assert set(AUTHORING_TOOLS) == {
        "new_screen",
        "patch_node",
        "add_node",
        "remove_node",
        "delete_draft",
        "convert_legacy",
        "request_push",
    }


# request_push copies a draft into a bundle for review; it never writes the store.
STORE_WRITING_TOOLS = tuple(name for name in AUTHORING_TOOLS if name != "request_push")


@pytest.mark.parametrize("tool", AUTHORING_TOOLS)
@pytest.mark.parametrize("draft_id", [LIBRARY_ID, "library/sr/absent"])
async def test_every_authoring_tool_refuses_a_library_id(
    settings: Settings, roots: tuple[Path, Path], httpx_mock, tool: str, draft_id: str
) -> None:
    before = [_tree(root) for root in roots]
    rev = _common.get_library_store(settings).rev("sr/overview")

    result = await _call_authoring_tool(tool, draft_id, rev)

    assert result["refused"] is True
    assert result["reason"] == "read_only_store"
    assert result["message"].startswith("read_only_store: ")
    assert result["draftId"] == draft_id
    assert [_tree(root) for root in roots] == before
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("tool", AUTHORING_TOOLS)
async def test_every_authoring_tool_refuses_the_bare_reserved_id(settings: Settings, httpx_mock, tool: str) -> None:
    with pytest.raises(ToolError, match="reserved"):
        await _call_authoring_tool(tool, "library", "0" * 64)
    assert httpx_mock.get_requests() == []


async def test_reading_tools_refuse_the_bare_reserved_id(settings: Settings) -> None:
    with pytest.raises(ToolError, match="reserved"):
        await get_screen("library")
    with pytest.raises(ToolError, match="reserved"):
        await check(draft_id="library")
    with pytest.raises(ToolError, match="reserved"):
        await screenshot.screenshot("library")


async def test_a_personal_write_still_works_beside_a_library(settings: Settings, roots: tuple[Path, Path]) -> None:
    result = await new_screen("mine/second", _doc("Second"))

    assert result["url"] == "http://127.0.0.1:8765/embed/drafts/mine/second"
    assert (roots[0] / "screens" / "mine" / "second.screen.json").is_file()
    assert not (roots[1] / "screens" / "mine").exists()


async def test_a_draft_ref_into_the_reserved_segment_is_unresolved_not_an_error(settings: Settings) -> None:
    """A ref that lands on ``library/...`` in the personal store must not break the check gate."""
    doc = _doc("Refers")
    doc["root"]["children"].append(_embed("e", "library/nope.screen.json"))

    result = await new_screen("top", doc)

    assert "refused" not in result
    assert [(w["nodeId"], w["code"], w.get("reason")) for w in result["warnings"]] == [
        ("e", "root_relative_ref", None),
        ("e", "unresolved_ref", "missing"),
    ]


@pytest.mark.parametrize("tool", STORE_WRITING_TOOLS)
async def test_an_unwritable_personal_store_refuses_with_the_same_reason(
    personal_only: Settings, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, httpx_mock, tool: str
) -> None:
    """A drafts tree that answers EROFS or EACCES at the write is a refusal, with no server path in it."""

    def unwritable(self, *args, **kwargs):
        raise ReadOnlyStore(f"{self.root}: Read-only file system")

    monkeypatch.setattr(DraftStore, "write_many", _raising(unwritable))
    monkeypatch.setattr(DraftStore, "delete", _raising(unwritable))
    httpx_mock.add_response(
        url="http://canopy.test/converter/v1/conversions", method="POST", json={"screen": _doc("c")}, is_optional=True
    )
    before = _tree(roots[0])
    rev = _common.get_store(personal_only).rev("mine/view")

    # convert_legacy reaches its write only for an id with nothing stored under it.
    result = await _call_authoring_tool(tool, "mine/fresh" if tool == "convert_legacy" else "mine/view", rev)

    assert (result["refused"], result["reason"]) == (True, "read_only_store")
    assert result["message"].startswith("read_only_store: ")
    assert str(roots[0]) not in json.dumps(result)
    assert _tree(roots[0]) == before


# --- unset: nothing changes ---------------------------------------------------------


async def test_without_a_library_library_is_an_ordinary_first_segment(
    personal_only: Settings, roots: tuple[Path, Path]
) -> None:
    assert _common.get_library_store(personal_only) is None

    result = await new_screen("library/mine", _doc("Mine too"))

    assert result["draftId"] == "library/mine"
    assert result["url"] == "http://127.0.0.1:8765/embed/drafts/library/mine"
    assert (roots[0] / "screens" / "library" / "mine.screen.json").is_file()
    assert (await get_screen("library/old"))["screen"]["metadata"]["title"] == "Shadow"
    assert [d["draftId"] for d in (await list_drafts())["drafts"]] == ["library/mine", "library/old", "mine/view"]
    deleted = await delete_draft("library/mine", result["rev"])
    assert deleted == {"draftId": "library/mine", "deleted": True}
    with pytest.raises(ToolError, match="no draft stored under 'library/sr/overview'"):
        await get_screen("library/sr/overview")


# --- convert_legacy siblings --------------------------------------------------------


def _legacy_tree(tmp_path: Path, httpx_mock) -> Path:
    """A legacy parent whose one embed points at ``library/sub``, with that source beside it."""
    parent = _put(tmp_path / "src" / "parent.bob", "<display/>")
    _put(tmp_path / "src" / "library" / "sub.bob", "<display/>")

    def convert_one(request):
        doc = _doc("converted")
        if b'filename="parent.bob"' in request.content:
            doc["root"]["children"].append(_embed("e", "library/sub.screen.json"))
        return httpx.Response(200, json={"screen": doc})

    httpx_mock.add_callback(
        convert_one, url="http://canopy.test/converter/v1/conversions", method="POST", is_reusable=True
    )
    return parent


async def test_convert_legacy_does_not_write_a_sibling_under_the_reserved_segment(
    settings: Settings, roots: tuple[Path, Path], tmp_path: Path, httpx_mock
) -> None:
    parent = _legacy_tree(tmp_path, httpx_mock)
    library_before = _tree(roots[1])

    result = await convert_legacy("top", path=str(parent), kind="bob")

    assert [d["draftId"] for d in result["drafts"]] == ["top"]
    assert len(httpx_mock.get_requests()) == 1
    assert not (roots[0] / "screens" / "library" / "sub.screen.json").exists()
    assert _tree(roots[1]) == library_before
    assert [(q["nodeId"], q["code"], q["reason"]) for q in result["queue"]] == [("e", "unresolved_ref", "missing")]


async def test_convert_legacy_writes_that_sibling_without_a_library(
    personal_only: Settings, roots: tuple[Path, Path], tmp_path: Path, httpx_mock
) -> None:
    parent = _legacy_tree(tmp_path, httpx_mock)

    result = await convert_legacy("top", path=str(parent), kind="bob")

    assert [d["draftId"] for d in result["drafts"]] == ["top", "library/sub"]
    assert (roots[0] / "screens" / "library" / "sub.screen.json").is_file()
