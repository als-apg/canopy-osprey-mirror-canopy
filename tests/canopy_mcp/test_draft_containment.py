"""A screen file under ``screens/`` that resolves outside the store is never read, listed or served."""

import json

import pytest
from fastmcp.exceptions import ToolError

from canopy_mcp.store import DraftStore, RevConflict, DraftNotFound, ReadOnlyStore
from canopy_mcp.tools import drafts, _common
from canopy_mcp.config import Settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.tools.check import run_check
from tests.canopy_mcp.synthetic_spec import write_spec

SECRET = {
    "schemaVersion": "1.0",
    "kind": "screen",
    "id": "secret",
    "metadata": {"title": "outside the store", "source": {"type": "native"}, "size": {"width": 1, "height": 1}},
    "macros": [],
    "root": {"id": "root", "type": "group", "props": {}, "geometry": {"x": 0, "y": 0, "width": 1, "height": 1}},
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(base_url="http://canopy.test", drafts_dir=tmp_path / "drafts", spec_dir=write_spec(tmp_path / "spec"))
    monkeypatch.setattr(drafts, "get_settings", lambda: s)
    _common.reset_stores()
    set_catalog(build_catalog({"group": {"consumes": [], "verbatim": False}}))
    yield s
    set_catalog(None)
    _common.reset_stores()


@pytest.fixture
def escaping_link(settings, tmp_path):
    """``screens/x.screen.json`` -> a readable screen file outside the drafts root."""
    outside = tmp_path / "outside" / "secret.screen.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps(SECRET))
    screens = settings.drafts_dir / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    (screens / "x.screen.json").symlink_to(outside)
    return outside


@pytest.fixture
def escaping_dir(settings, tmp_path):
    """``screens/sub`` -> a directory outside the drafts root holding ``y.screen.json``."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "y.screen.json").write_text(json.dumps(SECRET))
    screens = settings.drafts_dir / "screens"
    screens.mkdir(parents=True, exist_ok=True)
    (screens / "sub").symlink_to(outside, target_is_directory=True)
    return outside


def test_store_read_refuses_symlink_leaving_the_root(settings, escaping_link):
    store = DraftStore(settings.drafts_dir)
    with pytest.raises(DraftNotFound):
        store.read("x")
    assert store.rev("x") is None


def test_store_read_refuses_symlinked_directory_leaving_the_root(settings, escaping_dir):
    store = DraftStore(settings.drafts_dir)
    with pytest.raises(DraftNotFound):
        store.read("sub/y")


def test_list_and_manifest_skip_symlink_leaving_the_root(settings, escaping_link, escaping_dir):
    store = DraftStore(settings.drafts_dir)
    assert store.list() == []
    assert store.manifest() == {"screens": []}


def test_symlink_inside_the_root_is_still_read(settings):
    store = DraftStore(settings.drafts_dir)
    screens = settings.drafts_dir / "screens"
    screens.mkdir(parents=True)
    (screens / "real.screen.json").write_text(json.dumps(SECRET))
    (screens / "alias.screen.json").symlink_to(screens / "real.screen.json")
    assert store.read("alias")[0] == SECRET
    assert store.list() == ["alias", "real"]


async def test_get_screen_refuses_symlink_leaving_the_root(settings, escaping_link):
    with pytest.raises(ToolError, match="no draft stored"):
        await drafts.get_screen("x")


async def test_list_drafts_leaves_out_symlink_leaving_the_root(settings, escaping_link):
    assert (await drafts.list_drafts())["drafts"] == []


async def test_check_refuses_symlink_leaving_the_root(settings, escaping_link):
    with pytest.raises(ToolError, match="no draft stored"):
        await run_check(draft_id="x", settings=settings)


async def test_store_write_refuses_a_directory_leaving_the_root(settings, escaping_dir):
    store = DraftStore(settings.drafts_dir)
    before = (escaping_dir / "y.screen.json").read_bytes()
    for target in ("sub/y", "sub/new"):
        with pytest.raises(ReadOnlyStore):
            await store.write(target, SECRET, None)
    assert (escaping_dir / "y.screen.json").read_bytes() == before
    assert sorted(p.name for p in escaping_dir.iterdir()) == ["y.screen.json"]


async def test_store_write_refuses_all_when_one_draft_leaves_the_root(settings, escaping_dir):
    store = DraftStore(settings.drafts_dir)
    with pytest.raises(ReadOnlyStore):
        await store.write_many({"inside": (SECRET, None), "sub/y": (SECRET, None)})
    assert store.rev("inside") is None


async def test_store_create_never_writes_through_a_file_link_leaving_the_root(settings, escaping_link):
    store = DraftStore(settings.drafts_dir)
    before = escaping_link.read_bytes()
    with pytest.raises(RevConflict):
        await store.write("x", {"replaced": True}, None)
    assert escaping_link.read_bytes() == before


async def test_new_screen_leaves_the_outside_file_untouched(settings, escaping_dir):
    before = (escaping_dir / "y.screen.json").read_bytes()
    result = await drafts.new_screen("sub/y", dict(SECRET, id="y"))
    assert result["refused"] is True
    assert (escaping_dir / "y.screen.json").read_bytes() == before
