"""``list_collections``: the read-only agent view of every collection (FR10).

Runs over the checked-in fixture set: ``valid/facility`` as the facility store,
``valid/personal`` as the personal store and ``library/`` as the shared library.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastmcp import Client

from canopy_mcp.tools import collections as tool_module
from canopy_mcp.config import Settings
from canopy_mcp.server import AUTHORING_TOOLS, mcp, register_all
from canopy_mcp.tools._common import reset_stores

FIXTURES = Path(__file__).parent / "fixtures" / "collections"
FACILITY = FIXTURES / "valid" / "facility"
PERSONAL = FIXTURES / "valid" / "personal"
LIBRARY = FIXTURES / "library"


def _trace_dist(root: Path, *pages: str) -> Path:
    dist = root / "trace-dist"
    dist.mkdir()
    for page in pages:
        (dist / page).write_text("<!doctype html>", encoding="utf-8")
    return dist


def _drafts(root: Path) -> Path:
    drafts = root / "drafts"
    screen = drafts / "screens" / "bts" / "view.screen.json"
    screen.parent.mkdir(parents=True)
    screen.write_text(json.dumps({"metadata": {"title": "BTS draft"}}), encoding="utf-8")
    return drafts


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    built = Settings(
        drafts_dir=_drafts(tmp_path),
        library_dir=LIBRARY,
        facility_collections_dir=FACILITY,
        collections_dir=PERSONAL,
        trace_dist=_trace_dist(tmp_path, "index.html", "correlation.html"),
    )
    monkeypatch.setattr(tool_module, "get_settings", lambda: built)
    reset_stores()
    tool_module.reset_readers()
    yield built
    reset_stores()
    tool_module.reset_readers()


def _by_ref(result: dict) -> dict[str, dict]:
    return {collection["ref"]: collection for collection in result["collections"]}


async def test_lists_facility_first_then_personal_in_get_order(settings: Settings) -> None:
    result = await tool_module.list_collections()
    expected = json.loads((FIXTURES / "valid" / "list-body.json").read_text(encoding="utf-8"))
    assert [c["ref"] for c in result["collections"]] == [c["ref"] for c in expected["collections"]]


async def test_each_collection_carries_exactly_the_fr10_fields(settings: Settings) -> None:
    result = await tool_module.list_collections()
    for collection in result["collections"]:
        assert set(collection) == {
            "ref",
            "source",
            "name",
            "description",
            "default",
            "open_url",
            "available_count",
            "members",
        }
    control = _by_ref(result)["facility:control-assistant"]
    assert control["source"] == "facility"
    assert control["name"] == "Control assistant"
    assert control["description"].startswith("The storage ring overview")
    assert control["default"] is True
    personal = _by_ref(result)["personal:grid"]
    assert personal["source"] == "personal"
    assert personal["default"] is False
    assert personal["description"] == ""


async def test_open_url_opens_the_collection_through_the_embed_root(settings: Settings) -> None:
    result = await tool_module.list_collections()
    for collection in result["collections"]:
        base, _, query = collection["open_url"].partition("?")
        assert base == "http://127.0.0.1:8765/embed/" or base.endswith("/embed/")
        assert query.startswith("_canopyOpen=")
        assert unquote(query[len("_canopyOpen=") :]) == collection["ref"]


async def test_open_url_is_panel_relative_inside_osprey(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    inside = settings.model_copy(update={"panel_id": "canopy"})
    monkeypatch.setattr(tool_module, "get_settings", lambda: inside)
    result = await tool_module.list_collections()
    control = _by_ref(result)["facility:control-assistant"]
    assert control["open_url"].startswith("/panel/canopy/embed/?_canopyOpen=")
    screen = control["members"][0]
    assert screen["url"] == "/panel/canopy/embed/library/sr/overview"


async def test_screen_members_carry_key_title_and_url(settings: Settings) -> None:
    result = await tool_module.list_collections()
    overview = _by_ref(result)["facility:control-assistant"]["members"][0]
    assert set(overview) == {"key", "available", "title", "url", "ref"}
    assert overview["ref"] == "library/sr/overview"
    assert overview["key"] == "screen:library/sr/overview"
    assert overview["available"] is True
    assert overview["title"] == "sr/overview"  # the fixture screen has no metadata title
    assert overview["url"].endswith("/embed/library/sr/overview")


async def test_a_draft_screen_member_is_available_when_the_draft_exists(settings: Settings) -> None:
    result = await tool_module.list_collections()
    members = _by_ref(result)["personal:drafts"]["members"]
    draft = members[0]
    assert draft == {
        "key": "screen:drafts/bts/view",
        "available": True,
        "title": "BTS draft",
        "url": "http://127.0.0.1:8765/embed/drafts/bts/view",
        "ref": "drafts/bts/view",
    }
    with_macros = members[1]
    assert with_macros["key"] == "screen:library/sr/overview?S=SR01"
    assert with_macros["url"] == "http://127.0.0.1:8765/embed/library/sr/overview?S=SR01"
    assert (with_macros["ref"], with_macros["macros"]) == ("library/sr/overview", {"S": "SR01"})


async def test_a_missing_screen_is_unavailable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a library, no library/* member resolves; the draft one still does.
    no_library = settings.model_copy(update={"library_dir": None})
    monkeypatch.setattr(tool_module, "get_settings", lambda: no_library)
    reset_stores()
    result = await tool_module.list_collections()
    members = _by_ref(result)["personal:drafts"]["members"]
    assert [m["available"] for m in members] == [True, False, True]
    assert members[1]["title"] == "library/sr/overview"
    control = _by_ref(result)["facility:control-assistant"]
    assert control["members"][0]["available"] is False
    assert control["available_count"] == 2  # trace and correlation are configured


async def test_tool_members_carry_label_and_null_open_url(settings: Settings) -> None:
    result = await tool_module.list_collections()
    injector = _by_ref(result)["facility:injector"]["members"]
    trace = injector[2]
    assert trace == {
        "key": "tool:trace?pv=EG______HV_____AM00&pv=EG______HTR____AM00&range=-1h",
        "available": True,
        "label": "Gun high voltage and heater",
        "open_url": None,
        "tool": "trace",
        "query": "pv=EG______HV_____AM00&pv=EG______HTR____AM00&range=-1h",
    }
    unnamed = _by_ref(result)["personal:drafts"]["members"][2]
    assert unnamed == {"key": "tool:trace", "available": True, "label": "Trace", "open_url": None, "tool": "trace"}


async def test_a_tool_this_host_does_not_serve_is_unavailable(settings: Settings) -> None:
    result = await tool_module.list_collections()
    diagnostics = _by_ref(result)["facility:diagnostics"]
    waveform = diagnostics["members"][2]
    assert waveform["available"] is False  # the trace dist has no waveform.html
    assert waveform["label"] == "Waveform"
    assert diagnostics["available_count"] == 2
    blank = _by_ref(result)["personal:undocked"]["members"][3]
    assert blank == {
        "key": "tool:waveform?",
        "available": False,
        "label": "Waveform",
        "open_url": None,
        "tool": "waveform",
        "query": "",
    }


async def test_available_count_counts_available_members(settings: Settings) -> None:
    result = await tool_module.list_collections()
    for collection in result["collections"]:
        assert collection["available_count"] == sum(m["available"] for m in collection["members"])
    assert _by_ref(result)["facility:storage-ring"]["available_count"] == 3


async def test_no_stores_lists_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = Settings(drafts_dir=tmp_path / "drafts", collections_dir=tmp_path / "none")
    monkeypatch.setattr(tool_module, "get_settings", lambda: empty)
    reset_stores()
    tool_module.reset_readers()
    try:
        assert await tool_module.list_collections() == {"collections": []}
    finally:
        reset_stores()
        tool_module.reset_readers()
    assert not (tmp_path / "none").exists()  # listing writes nothing


async def test_list_collections_is_registered_and_read_only(settings: Settings) -> None:
    register_all()
    assert "list_collections" not in AUTHORING_TOOLS
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "list_collections" in names
        result = await client.call_tool("list_collections", {})
    assert [c["ref"] for c in result.structured_content["collections"]][:1] == ["facility:control-assistant"]


# A14: the agent answers in plain language. The tool description and the skill
# row both carry this instruction; RST double backticks read as single ones.
PLAIN_LANGUAGE = (
    "Answer with collection names and member counts, render `open_url` as a link, "
    "and never quote field names to the operator."
)
SKILL_MD = Path(tool_module.__file__).parent.parent / "skill" / "SKILL.md"


def _normalised(text: str) -> str:
    return " ".join(text.replace("``", "`").split())


def test_docstring_tells_the_agent_to_answer_in_plain_language() -> None:
    doc = _normalised(tool_module.list_collections.__doc__ or "")
    assert PLAIN_LANGUAGE in doc
    for field in ("ref", "source", "available_count", "open_url", "available"):
        assert f"`{field}`" in doc  # the Returns paragraph keeps its field names


def test_skill_row_tells_the_agent_to_answer_in_plain_language() -> None:
    rows = [
        line for line in SKILL_MD.read_text(encoding="utf-8").splitlines() if line.startswith("| `list_collections` |")
    ]
    assert len(rows) == 1
    assert PLAIN_LANGUAGE in rows[0]
