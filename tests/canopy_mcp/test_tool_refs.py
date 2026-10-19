"""The ``ref`` fields on ``list_drafts``, ``list_collections``, ``get_screen`` and ``new_screen``.

Each ref (and each tool member's ``open_tool`` arguments) must be exactly what the
workspace command vectors in ``fixtures/workspace/commands/`` accept: a screen ref
is ``<category>/<local id>`` for ``open``, a collection ref ``<source>:<id>`` for
``open_collection``, a tool member ``{tool, query?}`` for ``open_tool``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from canopy_mcp.config import Settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.tools import drafts
from canopy_mcp.tools import collections as collections_tool
from canopy_mcp.tools._common import reset_stores
from canopy_mcp.tools.drafts import draft_ref, get_screen, new_screen, list_drafts, screen_ref
from tests.canopy_mcp.synthetic_spec import write_spec

FIXTURES = Path(__file__).parent / "fixtures"
COMMANDS = FIXTURES / "workspace" / "commands"
CONTEXT = FIXTURES / "workspace" / "context.json"
COLLECTIONS = FIXTURES / "collections"


def _vector(verb: str) -> dict[str, Any]:
    return json.loads((COMMANDS / f"{verb}.json").read_text(encoding="utf-8"))


def _vector_screen_refs() -> set[str]:
    """Every screen ref the ``open`` vector and the shared context use."""
    refs = {case["command"]["args"]["ref"] for case in _vector("open")["cases"]}
    refs |= set(json.loads(CONTEXT.read_text(encoding="utf-8"))["context"]["manifest"])
    return refs


SCREEN_REF = re.compile(r"(library|drafts)/[^/?#\s][^?#\s]*")
COLLECTION_REF = re.compile(r"(facility|personal):[^:\s]+")


def assert_open_args(args: dict[str, Any]) -> None:
    """``args`` is a command the ``open`` vector accepts (ref, optional string macros)."""
    spec = _vector("open")["args"]
    assert set(args) <= set(spec) - {"mode", "key"}
    assert isinstance(args["ref"], str) and SCREEN_REF.fullmatch(args["ref"]), args
    for name, value in args.get("macros", {}).items():
        assert isinstance(name, str) and isinstance(value, str)


def assert_open_collection_args(args: dict[str, Any]) -> None:
    spec = _vector("open_collection")["args"]
    assert set(args) <= set(spec)
    assert isinstance(args["ref"], str) and COLLECTION_REF.fullmatch(args["ref"]), args
    for case in _vector("open_collection")["cases"]:  # the vector's own refs share the form
        assert COLLECTION_REF.fullmatch(case["command"]["args"]["ref"])


def assert_open_tool_args(args: dict[str, Any]) -> None:
    spec = _vector("open_tool")["args"]
    assert set(args) <= set(spec)
    tools = spec["tool"].split(",")[0].split(" | ")
    assert args["tool"] in [t.strip() for t in tools]
    if "query" in args:
        assert isinstance(args["query"], str)


def test_the_screen_ref_grammar_matches_every_vector_ref() -> None:
    for ref in _vector_screen_refs():
        assert SCREEN_REF.fullmatch(ref), ref


def test_screen_ref_formats_category_and_local_id() -> None:
    assert screen_ref("library", "s3-promo/promotion") == "library/s3-promo/promotion"
    assert screen_ref("drafts", "beam-tune") == "drafts/beam-tune"
    assert screen_ref("library", "s3-promo/promotion") in _vector_screen_refs()
    assert screen_ref("drafts", "beam-tune") in _vector_screen_refs()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    dist = tmp_path / "trace-dist"
    dist.mkdir()
    for page in ("index.html", "correlation.html"):
        (dist / page).write_text("<!doctype html>", encoding="utf-8")
    draft = tmp_path / "drafts" / "screens" / "bts" / "view.screen.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"metadata": {"title": "BTS draft"}}), encoding="utf-8")
    built = Settings(
        drafts_dir=tmp_path / "drafts",
        library_dir=COLLECTIONS / "library",
        facility_collections_dir=COLLECTIONS / "valid" / "facility",
        collections_dir=COLLECTIONS / "valid" / "personal",
        trace_dist=dist,
        spec_dir=write_spec(tmp_path / "spec"),
    )
    monkeypatch.setattr(drafts, "get_settings", lambda: built)
    monkeypatch.setattr(collections_tool, "get_settings", lambda: built)
    reset_stores()
    collections_tool.reset_readers()
    set_catalog(build_catalog({"group": {"consumes": [], "verbatim": False}}))
    yield built
    set_catalog(None)
    reset_stores()
    collections_tool.reset_readers()


def test_draft_ref_prefixes_a_personal_draft_and_keeps_a_library_screen(settings: Settings) -> None:
    assert draft_ref(settings, "bts/view") == "drafts/bts/view"
    assert draft_ref(settings, "library/sr/overview") == "library/sr/overview"


def test_draft_ref_without_a_library_is_a_draft(settings: Settings) -> None:
    bare = settings.model_copy(update={"library_dir": None})
    assert draft_ref(bare, "library/sr/overview") == "drafts/library/sr/overview"


async def test_list_drafts_carries_a_ref_open_accepts(settings: Settings) -> None:
    listed = (await list_drafts())["drafts"]
    refs = {entry["draftId"]: entry["ref"] for entry in listed}
    assert refs["bts/view"] == "drafts/bts/view"
    assert refs["library/s3-promo/promotion"] == "library/s3-promo/promotion"
    assert refs["library/common/KnobPanel"] == "library/common/KnobPanel"
    for entry in listed:
        assert set(entry) == {"draftId", "title", "path", "rev", "url", "ref"}
        assert_open_args({"ref": entry["ref"]})
        # the ref names the same screen as the url
        assert entry["url"].endswith("/embed/" + entry["ref"])


async def test_list_drafts_refs_match_the_vector_refs_for_the_same_screens(settings: Settings) -> None:
    listed = {entry["ref"] for entry in (await list_drafts())["drafts"]}
    shared = listed & _vector_screen_refs()
    assert {"library/s3-promo/promotion", "library/common/KnobPanel", "library/bts/BTSView"} <= shared


async def test_get_screen_carries_a_ref_open_accepts(settings: Settings) -> None:
    for draft_id, ref in (("bts/view", "drafts/bts/view"), ("library/sr/overview", "library/sr/overview")):
        got = await get_screen(draft_id)
        assert got["ref"] == ref
        assert set(got) == {"draftId", "rev", "url", "ref", "screen"}
        assert_open_args({"ref": got["ref"]})


def _doc(title: str = "t") -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": title, "source": {"type": "native"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": {"id": "root", "type": "group", "props": {}, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}},
    }


async def test_new_screen_carries_a_ref_open_accepts(settings: Settings) -> None:
    out = await new_screen("beam-tune", _doc())
    assert "refused" not in out, out
    assert out["ref"] == "drafts/beam-tune"
    assert out["ref"] in _vector_screen_refs()
    assert_open_args({"ref": out["ref"]})
    assert (await get_screen("beam-tune"))["ref"] == out["ref"]


async def test_new_screen_refusal_has_no_ref(settings: Settings) -> None:
    out = await new_screen("library/sr/overview", _doc())
    assert out["refused"] is True
    assert "ref" not in out


def _members(result: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    return next(c for c in result["collections"] if c["ref"] == ref)["members"]


async def test_list_collections_refs_are_open_collection_args(settings: Settings) -> None:
    result = await collections_tool.list_collections()
    assert result["collections"]
    for collection in result["collections"]:
        assert_open_collection_args({"ref": collection["ref"]})


async def test_list_collections_screen_members_carry_an_open_ref(settings: Settings) -> None:
    result = await collections_tool.list_collections()
    draft, with_macros, _ = _members(result, "personal:drafts")
    assert draft["ref"] == "drafts/bts/view"
    assert "macros" not in draft
    assert with_macros["ref"] == "library/sr/overview"
    assert with_macros["macros"] == {"S": "SR01"}
    for collection in result["collections"]:
        for member in collection["members"]:
            if member["key"].startswith("screen:"):
                assert "tool" not in member
                args = {k: member[k] for k in ("ref", "macros") if k in member}
                assert_open_args(args)
                # the ref is the screen part of the member key
                assert member["key"][len("screen:") :].partition("?")[0] == member["ref"]


async def test_list_collections_tool_members_carry_open_tool_args(settings: Settings) -> None:
    result = await collections_tool.list_collections()
    injector_trace = _members(result, "facility:injector")[2]
    assert injector_trace["tool"] == "trace"
    assert injector_trace["query"] == "pv=EG______HV_____AM00&pv=EG______HTR____AM00&range=-1h"
    unnamed = _members(result, "personal:drafts")[2]
    assert unnamed["tool"] == "trace"
    assert "query" not in unnamed
    for collection in result["collections"]:
        for member in collection["members"]:
            if member["key"].startswith("tool:"):
                assert "ref" not in member
                assert_open_tool_args({k: member[k] for k in ("tool", "query") if k in member})


async def test_list_collections_and_list_drafts_agree_on_a_screen_ref(settings: Settings) -> None:
    listed = {entry["ref"] for entry in (await list_drafts())["drafts"]}
    result = await collections_tool.list_collections()
    member_refs = {m["ref"] for c in result["collections"] for m in c["members"] if "ref" in m and m["available"]}
    assert member_refs
    assert member_refs <= listed
