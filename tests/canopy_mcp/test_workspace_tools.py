"""``get_workspace`` / ``set_workspace``: the workspace hub as the agent reaches it, through MCP.

The shared refusal and scenario vectors (``fixtures/workspace/``) are replayed with every agent
step sent through a FastMCP client to the registered tools, the window steps going to the same
hub; then what only the tools add: the sidebar read from the collections state, the clamped
wait, ``pending`` on a timeout and at shutdown, a result posted over the window's HTTP route,
and the opt-in that decides whether the two tools are offered at all.
"""

from __future__ import annotations

import re
import json
import time
import shutil
import asyncio
from typing import Any
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from canopy_mcp import workspace as workspace_module
from canopy_mcp.app import create_app
from canopy_mcp.tools import workspace as workspace_tools
from canopy_mcp.tools import collections as collections_tools
from canopy_mcp.config import Settings
from canopy_mcp.server import (
    AUTHORING_TOOLS,
    WORKSPACE_TOOLS,
    mcp,
    register_all,
    set_authoring,
    registered_tools,
    workspace_snapshot,
    set_workspace_tools,
)
from canopy_mcp.workspace import AS, VERBS, PROTOCOL, REFUSAL_REASONS, Hub

from .test_workspace_hub import SAMPLE, REFUSALS, SCENARIOS, Replay, matches

COLLECTIONS = Path(__file__).parent / "fixtures" / "collections" / "valid"
PHRASE = "the CANOPY workspace: tabs and tiles inside the CANOPY panel"
FOCUS = {"cmd": "focus", "args": {"key": "library/linac/LinacView?SR=1"}}


def _names() -> set[str]:
    return set(registered_tools())


@pytest.fixture(autouse=True)
def restore_workspace_tools() -> Any:
    """Leave the process-wide tool set as the test found it: offered or not."""
    register_all()
    offered = set(WORKSPACE_TOOLS) <= _names()
    yield
    set_workspace_tools(offered)


@pytest.fixture
def sidebar_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Facility and personal collections from the shared fixtures; the personal dir is a copy the test may write."""
    personal = tmp_path / "personal"
    shutil.copytree(COLLECTIONS / "personal", personal)
    built = Settings(
        drafts_dir=tmp_path / "drafts",
        facility_collections_dir=COLLECTIONS / "facility",
        collections_dir=personal,
    )
    monkeypatch.setattr(workspace_tools, "get_settings", lambda: built)
    collections_tools.reset_readers()
    yield personal
    collections_tools.reset_readers()


@pytest.fixture
def offered(sidebar_dirs: Path) -> None:
    set_workspace_tools(True)


def _use(monkeypatch: pytest.MonkeyPatch, hub: Hub) -> Hub:
    monkeypatch.setattr(workspace_module, "_hub", hub)
    return hub


class ToolReplay(Replay):
    """The hub replay with every agent step sent to the MCP tools."""

    def __init__(self, client: Client) -> None:
        super().__init__()
        self.client = client

    async def step(self, step: dict[str, Any]) -> Any:
        if step["actor"] != "agent":
            return await super().step(step)
        args = {k: step[k] for k in ("commands", "base", "batch_id", "wait_s") if k in step}
        result = await self.client.call_tool(step["do"], args)
        return result.structured_content


async def _replay(monkeypatch: pytest.MonkeyPatch, steps: list[dict[str, Any]], where: str) -> None:
    async with Client(mcp) as client:
        replay = ToolReplay(client)
        _use(monkeypatch, replay.hub)
        await replay.run(steps, where)


def _refusal_cases():
    for name, doc in REFUSALS.items():
        for case in doc["cases"]:
            yield pytest.param(case, id=f"{name}:{case['name']}")


# --- the shared vectors, through MCP ------------------------------------------------------


def test_workspace_tools_refusal_vectors_cover_every_reason():
    assert {doc["reason"] for doc in REFUSALS.values()} == set(REFUSAL_REASONS)


@pytest.mark.usefixtures("offered")
@pytest.mark.parametrize("case", list(_refusal_cases()))
async def test_workspace_tools_replay_refusal(case: dict, monkeypatch: pytest.MonkeyPatch):
    await _replay(monkeypatch, case["steps"], case["name"])


@pytest.mark.usefixtures("offered")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_workspace_tools_replay_scenario(name: str, monkeypatch: pytest.MonkeyPatch):
    await _replay(monkeypatch, SCENARIOS[name]["steps"], f"scenarios/{name}")


# --- helpers for the hand-written cases ------------------------------------------------------


def _claimed(monkeypatch: pytest.MonkeyPatch, shown: bool = True):
    """A real-clock hub with window w-A claimed (seq 41) and its stream open."""
    hub = _use(monkeypatch, Hub(epoch="e1"))
    hub.claim("w-A", 41, shown, SAMPLE["state"])
    stream = hub.connect("w-A")
    assert stream is not None and stream.drain()[0]["event"] == "hello"
    return hub, stream


async def _call(client: Client, name: str, **args: Any) -> dict[str, Any]:
    return (await client.call_tool(name, args)).structured_content


# --- the document ------------------------------------------------------------------------------


@pytest.mark.usefixtures("offered")
async def test_workspace_tools_get_workspace_returns_the_document_with_the_sidebar(
    sidebar_dirs: Path, monkeypatch: pytest.MonkeyPatch
):
    (sidebar_dirs / ".state.json").write_text(
        json.dumps(
            {
                "ticked": ["facility:injector", "personal:drafts", "facility:not-listed"],
                "unticked": ["facility:diagnostics"],
                "pinned": [
                    {"screen": "library/linac/LinacView", "macros": {"SR": "1"}},
                    {"tool": "trace", "query": "pv=SR:DCCT", "name": "Beam current"},
                ],
                "recent": [{"tool": "waveform"}, {"screen": "drafts/beam-tune"}],
            }
        ),
        encoding="utf-8",
    )
    _claimed(monkeypatch)
    async with Client(mcp) as client:
        doc = await _call(client, "get_workspace")
    assert doc["protocol"] == PROTOCOL == 1
    assert doc["epoch"] == "e1" and doc["activeWindow"] == "w-A" and doc["connected"] is True
    assert doc["seq"] == 41 and doc["shown"] is True and doc["truncated"] is False
    assert matches({"tabs": SAMPLE["state"]["tabs"], "active": SAMPLE["state"]["active"]}, doc["state"])
    assert doc["pending"] == [] and doc["results"] == []
    # (default control-assistant + ticked) minus unticked, in list order; an unlisted tick adds nothing.
    assert doc["collections"] == {"shown": ["facility:control-assistant", "facility:injector", "personal:drafts"]}
    assert doc["sidebar"] == {
        "pinned": [{"ref": "library/linac/LinacView", "macros": {"SR": "1"}}, {"tool": "trace", "query": "pv=SR:DCCT"}],
        "recent": [{"tool": "waveform"}, {"ref": "drafts/beam-tune"}],
    }
    assert "batch" not in doc


@pytest.mark.usefixtures("offered")
async def test_workspace_tools_without_a_window_or_a_state_file(monkeypatch: pytest.MonkeyPatch):
    _use(monkeypatch, Hub(epoch="e1"))
    async with Client(mcp) as client:
        doc = await _call(client, "get_workspace")
        sent = await _call(client, "set_workspace", commands=[FOCUS], wait_s=0)
    assert doc["activeWindow"] is None and doc["connected"] is False and doc["state"] is None
    assert doc["collections"] == {"shown": ["facility:control-assistant"]}
    assert doc["sidebar"] == {"pinned": [], "recent": []}
    assert sent["batch"] == {
        "id": None,
        "status": "refused",
        "reason": "no_window",
        "message": sent["batch"]["message"],
        "results": [],
    }
    assert sent["collections"] == doc["collections"] and sent["protocol"] == 1


# --- set_workspace -----------------------------------------------------------------------------


@pytest.mark.usefixtures("offered")
async def test_workspace_tools_batch_id_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    hub, stream = _claimed(monkeypatch)
    async with Client(mcp) as client:
        first = await _call(client, "set_workspace", commands=[FOCUS], wait_s=0)
        assert first["batch"] == {"id": "b-e1-1", "status": "pending", "results": []}
        again = await _call(client, "set_workspace", commands=[FOCUS, FOCUS], batch_id="b-e1-1", wait_s=0)
        assert again["batch"] == first["batch"]
        assert again["pending"] == ["c-e1-1"]  # the repeat enqueued nothing
        [batch] = [e for e in stream.drain() if e["event"] == "batch"]
        hub.post_result("w-A", "b-e1-1", 42, SAMPLE["state"], [{"id": "c-e1-1", "status": "applied", "changed": False}])
        resolved = await _call(client, "set_workspace", commands=[FOCUS], batch_id="b-e1-1", wait_s=0)
    assert batch["data"]["commands"] == [{"id": "c-e1-1", **FOCUS}]
    assert resolved["batch"] == {
        "id": "b-e1-1",
        "status": "done",
        "results": [{"id": "c-e1-1", "status": "applied", "changed": False}],
    }
    assert resolved["pending"] == [] and resolved["seq"] == 42


@pytest.mark.usefixtures("offered")
async def test_workspace_tools_pending_on_timeout(monkeypatch: pytest.MonkeyPatch):
    _claimed(monkeypatch)
    async with Client(mcp) as client:
        started = time.monotonic()
        sent = await _call(client, "set_workspace", commands=[FOCUS], wait_s=0.3)
        waited = time.monotonic() - started
    assert sent["batch"] == {"id": "b-e1-1", "status": "pending", "results": []}
    assert sent["pending"] == ["c-e1-1"]
    assert 0.25 <= waited < 3


@pytest.mark.usefixtures("offered")
@pytest.mark.parametrize(("asked", "waits"), [(60, 10.0), (10, 10.0), (2.5, 2.5), (0, None), (-4, None)])
async def test_workspace_tools_wait_is_clamped_to_ten_seconds(
    asked: float, waits: float | None, monkeypatch: pytest.MonkeyPatch
):
    hub, _ = _claimed(monkeypatch)
    seen: list[float] = []

    async def wait(batch_id: str, wait_s: float) -> dict[str, Any]:
        seen.append(wait_s)
        return hub.batch(batch_id)

    monkeypatch.setattr(hub, "wait", wait)
    async with Client(mcp) as client:
        sent = await _call(client, "set_workspace", commands=[FOCUS], wait_s=asked)
    assert sent["batch"]["status"] == "pending"
    assert seen == ([] if waits is None else [waits])


def test_workspace_tools_clamp_wait():
    assert workspace_tools.clamp_wait(float("nan")) == 0.0
    assert workspace_tools.clamp_wait(float("inf")) == 10.0
    assert workspace_tools.clamp_wait(-0.1) == 0.0
    assert workspace_tools.clamp_wait(4) == 4.0


@pytest.mark.usefixtures("offered")
async def test_workspace_tools_close_returns_pending_to_a_waiter(monkeypatch: pytest.MonkeyPatch):
    hub, _ = _claimed(monkeypatch)
    async with Client(mcp) as client:
        call = asyncio.create_task(_call(client, "set_workspace", commands=[FOCUS], wait_s=10))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        hub.close()
        sent = await asyncio.wait_for(call, 2)
    assert time.monotonic() - started < 1
    assert sent["batch"] == {"id": "b-e1-1", "status": "pending", "results": []}


@pytest.mark.usefixtures("sidebar_dirs")
async def test_workspace_tools_results_after_a_window_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The waiting tool wakes with the results and the state the window POSTs to its result route."""
    app = create_app(Settings(drafts_dir=tmp_path / "drafts", workspace=True), startup_hooks=[])
    hub, stream = _claimed(monkeypatch)
    posted_state = json.loads(json.dumps(SAMPLE["state"]))
    posted_state["active"] = posted_state["tabs"][0]["key"]
    results = [
        {"id": "c-e1-1", "status": "applied", "changed": True},
        {"id": "c-e1-2", "status": "rejected", "reason": "unknown_target"},
    ]
    commands = [FOCUS, {"cmd": "focus", "args": {"key": "library/nope/Gone"}}]
    async with (
        Client(mcp) as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as window,
    ):
        call = asyncio.create_task(_call(client, "set_workspace", commands=commands, base={"window": "w-A", "seq": 41}))
        event = await stream.next(2)
        while event is not None and event["event"] != "batch":
            event = await stream.next(2)
        assert event is not None and event["data"]["batch"] == "b-e1-1"
        assert event["data"]["base"] == {"window": "w-A", "seq": 41}
        response = await window.post(
            "/workspace/active/w-A/result",
            json={"batch": "b-e1-1", "seq": 42, "state": posted_state, "results": results},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 204, response.text
        sent = await asyncio.wait_for(call, 3)
    assert sent["batch"] == {"id": "b-e1-1", "status": "done", "results": results}
    assert sent["seq"] == 42 and sent["state"]["active"] == posted_state["active"]
    assert sent["pending"] == []
    assert [(r["id"], r["batch"], r["window"], r["cmd"], r["status"]) for r in sent["results"]] == [
        ("c-e1-1", "b-e1-1", "w-A", "focus", "applied"),
        ("c-e1-2", "b-e1-1", "w-A", "focus", "rejected"),
    ]
    assert list(sent)[-1] == "batch" and "collections" in sent and "sidebar" in sent


# --- the opt-in ------------------------------------------------------------------------------------


def _build(tmp_path: Path, **overrides: Any):
    return create_app(Settings(drafts_dir=tmp_path / "drafts", **overrides), startup_hooks=[])


def test_workspace_tools_are_their_own_set_not_authoring():
    assert WORKSPACE_TOOLS == ("get_workspace", "set_workspace")
    assert not set(WORKSPACE_TOOLS) & set(AUTHORING_TOOLS)


async def test_workspace_tools_absent_with_the_opt_in_off(tmp_path: Path):
    _build(tmp_path)
    assert not set(WORKSPACE_TOOLS) & _names()
    async with Client(mcp) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert not set(WORKSPACE_TOOLS) & listed
        with pytest.raises(ToolError):
            await client.call_tool("set_workspace", {"commands": [FOCUS]})
    assert set(workspace_snapshot()) == set(WORKSPACE_TOOLS)


async def test_workspace_tools_offered_with_the_opt_in_on_and_the_same_objects(tmp_path: Path):
    _build(tmp_path, workspace=True)
    before = registered_tools()
    _build(tmp_path)
    _build(tmp_path)
    _build(tmp_path, workspace=True)
    _build(tmp_path, workspace=True)
    after = registered_tools()
    assert set(WORKSPACE_TOOLS) <= set(after)
    assert len(after) == len(before)
    assert all(after[name] is before[name] for name in WORKSPACE_TOOLS)


async def test_workspace_tools_and_authoring_switch_independently(tmp_path: Path):
    try:
        _build(tmp_path, workspace=True, authoring=False)
        names = _names()
        assert set(WORKSPACE_TOOLS) <= names and not set(AUTHORING_TOOLS) & names
        _build(tmp_path, workspace=False, authoring=True)
        names = _names()
        assert set(AUTHORING_TOOLS) <= names and not set(WORKSPACE_TOOLS) & names
    finally:
        set_authoring(True)


def test_workspace_tools_register_at_import():
    """The set exists without building an app: each name is a tool the module decorated."""
    set_workspace_tools(True)
    tools = registered_tools()
    for name in WORKSPACE_TOOLS:
        assert callable(getattr(workspace_tools, name))
        assert name in tools


def test_workspace_tools_descriptions_name_the_canopy_workspace():
    set_workspace_tools(True)
    tools = registered_tools()
    for name in WORKSPACE_TOOLS:
        assert PHRASE in tools[name].description


def test_workspace_tools_parameters():
    set_workspace_tools(True)
    schema = registered_tools()["set_workspace"].parameters
    assert schema["required"] == ["commands"]
    assert set(schema["properties"]) == {"commands", "base", "batch_id", "wait_s"}
    assert schema["properties"]["wait_s"]["default"] == 5.0
    assert registered_tools()["get_workspace"].parameters.get("properties", {}) == {}


# --- the text the agent reads ------------------------------------------------------------------

README = Path(__file__).parents[2] / "src" / "canopy_mcp" / "README.md"
RETIRED = ("place", "remove_tile", "polite", "append", "mode", "beside", "docked")


def _texts() -> tuple[str, str, str]:
    """The ``get_workspace`` description, the ``set_workspace`` description and its ``commands`` text."""
    set_workspace_tools(True)
    tools = registered_tools()
    commands = tools["set_workspace"].parameters["properties"]["commands"]["description"]
    return tools["get_workspace"].description, tools["set_workspace"].description, commands


def _readme_verbs() -> list[str]:
    """The verbs the README's verb table lists, in row order."""
    text = README.read_text(encoding="utf-8")
    table = text.split("| Verb | Arguments | Note |", 1)[1].split("\n\n", 1)[0]
    verbs: list[str] = []
    for row in table.splitlines()[2:]:
        cell = row.split("|")[1]
        verbs += [part.strip().strip("`") for part in cell.split(",")]
    return verbs


def test_workspace_tools_commands_text_names_every_verb_and_its_arguments():
    _, _, commands = _texts()
    for verb, (required, optional) in VERBS.items():
        assert re.search(rf"\b{verb}\b", commands), verb
        for name in required | optional:
            assert re.search(rf"\b{name}\b", commands), f"{verb}.{name}"
    for form in ("new", "tab", "tile", "centre", "edge", "border", "outer", "index"):
        assert re.search(rf"\b{form}\b", commands), form
    for value in AS:
        assert value in commands


def test_workspace_tools_text_names_no_retired_verb_or_argument():
    read, write, commands = _texts()
    readme = README.read_text(encoding="utf-8").split("## The workspace", 1)[1].split("### Developer recipe", 1)[0]
    for text in (read, write, commands):
        for word in RETIRED:
            assert not re.search(rf"\b{word}\b", text), word
    for word in (*RETIRED, "close"):  # the README's prose may say "beside"; a retired name is in backticks
        assert f"`{word}`" not in readme and f"`{word}?`" not in readme, word
    for text in (read, write, commands, readme):
        assert "layout: true" not in text  # open_collection's boolean layout is retired for as
    # The verb close is retired in favour of close_tab.
    assert not re.search(r"\bclose\b", commands.replace("close_", "")), "close"
    assert "open_collection {ref, as?" in commands


def test_workspace_tools_text_states_the_tile_rules():
    read, write, commands = _texts()
    joined = " ".join((read, write, commands))
    assert "a new tab in the focused tile" in commands
    assert re.search(r"close_others / close_right \{key\}[^.]*within the tab's tile", commands)
    assert "base" in commands and "tile id" in commands
    for text in (read, write):
        assert "state.layout" in text
    assert "tile id" in read and "showing" in read
    assert "null" in read and "below two" not in joined


def test_workspace_tools_readme_lists_exactly_the_hub_verbs():
    verbs = _readme_verbs()
    assert sorted(verbs) == sorted(VERBS)
    text = README.read_text(encoding="utf-8")
    assert f"closed vocabulary of {len(VERBS)} verbs" in text
    section = text.split("## The workspace", 1)[1].split("### Developer recipe", 1)[0]
    assert "state.layout" in section and "within the tab's tile" in section
