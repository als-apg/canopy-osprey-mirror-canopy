"""Every canopy_mcp tool, called over MCP against the running host API.

Live: needs ``CANOPY_LIVE=1`` and a running local stack (the canopy API on
``CANOPY_BASE_URL``, default ``http://127.0.0.1:8000``, with no PV directory
adapter). The key is ``CANOPY_API_KEY`` or, failing that, the key in the file
``CANOPY_READ_KEY_FILE`` names; the tests are skipped without either. The legacy corpus is read
from ``PHOEBUS_ACC`` (default ``~/code/phoebus-acc``) and the real viewer build
from ``CANOPY_VIEWER_DIST``; the screenshot test is skipped without one.

The canopy_mcp app runs in this process on a free port with its real startup
hooks (widget catalog, screenshot Chromium), and every tool is called through a
``fastmcp.Client`` on its ``/mcp`` endpoint. The tools read the process-wide
settings on every call, so each test points ``CANOPY_DRAFTS_DIR`` at its own
empty directory; only the screenshot test uses the directory the app's viewer
routes were built on.

Covered: SC3a (BTSView queue), SC3c (Cavity1Water tanks), SC5, SC6, SC7 and the
direct-request halves of SC8 (``PUT /hare/v1/pvs/X`` and
``POST /squirrel/x/restore`` answer 405).
"""

from __future__ import annotations

import os
import copy
import json
import time
import shutil
import socket
import threading
import http.client
from typing import Any
from pathlib import Path
from collections import Counter
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastmcp import Client
from mcp.types import ImageContent

from canopy_mcp import catalog as catalog_mod
from canopy_mcp.app import create_app
from canopy_mcp.tools import drafts, convert
from canopy_mcp.config import get_settings
from canopy_mcp.check.lint import RACCOON_VALIDATE_PATH, check_lint
from canopy_mcp.check.refs import resolve_ref
from tests.canopy_mcp.fixtures.convert import mocks

pytestmark = pytest.mark.live

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
SPEED_FIXTURE = FIXTURES / "speed-100-50-20.screen.json"
TWO_UNKNOWN = FIXTURES / "two-unknown.screen.json"
REFS_DRAFTS = FIXTURES / "refs" / "drafts"
ACC = Path(os.path.expanduser(os.environ.get("PHOEBUS_ACC", "~/code/phoebus-acc")))

EXPECTED_TOOLS = {
    "add_node",
    "check",
    "convert_legacy",
    "delete_draft",
    "get_schema",
    "get_screen",
    "get_widget",
    "list_drafts",
    "list_widgets",
    "new_screen",
    "patch_node",
    "read_pv",
    "remove_node",
    "screenshot",
    "search_pvs",
}

BTS_ID = "bts/BTSView"
BTS_DRAFTS = ["bts/BTSView", "bts/embedded/_KnobDevice", "bts/embedded/_bpm", "bts/embedded/_tv", "common/KnobPanel"]
BTS_TITLES = {
    "bts/BTSView": "BTSView",
    "bts/embedded/_KnobDevice": "_KnobDevice",
    "bts/embedded/_bpm": "_bpm",
    "bts/embedded/_tv": "_tv",
    "common/KnobPanel": "KnobPanel",
}


def _api_key() -> str:
    """``CANOPY_API_KEY``, else the key in the file ``CANOPY_READ_KEY_FILE`` names; skip without either."""
    key = os.environ.get("CANOPY_API_KEY")
    if key:
        return key
    key_file = os.environ.get("CANOPY_READ_KEY_FILE")
    if not key_file:
        pytest.skip("live test: set CANOPY_API_KEY or CANOPY_READ_KEY_FILE")
    return Path(key_file).read_text().strip()


def _bts_args() -> dict[str, Any]:
    """SC3a's exact ``convert_legacy`` call, with ``~/code/phoebus-acc`` read from ``PHOEBUS_ACC``."""
    return {
        "id": BTS_ID,
        "path": str(ACC / "bts" / "BTSView.bob"),
        "kind": "bob",
        "siblings": {
            "embedded/_KnobDevice.bob": str(ACC / "common" / "embedded" / "_KnobDevice.bob"),
            "embedded/_bpm.bob": str(ACC / "gtb" / "common" / "embedded" / "_bpm.bob"),
            "embedded/_tv.bob": str(ACC / "bts" / "embedded" / "_tv.bob"),
        },
    }


# --- the app on a real port ------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """``create_app`` with its default startup hooks, served by uvicorn in a thread."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = create_app(get_settings(), self_url=self.base_url)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.monotonic() + 60
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("canopy_mcp did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


def _use_drafts_dir(mp: pytest.MonkeyPatch, path: Path) -> None:
    mp.setenv("CANOPY_DRAFTS_DIR", str(path))
    get_settings.cache_clear()
    drafts.reset_stores()


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Server]:
    for path in (ACC / "bts" / "BTSView.bob", ACC / "sr" / "srrf" / "Cavity1Water.bob"):
        if not path.is_file():
            pytest.fail(f"legacy corpus file {path} not found; set PHOEBUS_ACC")
    mp = pytest.MonkeyPatch()
    mp.setenv("CANOPY_API_KEY", _api_key())
    mp.setenv("CANOPY_PV_MODE", "hare")
    mp.delenv("CANOPY_PANEL_ID", raising=False)
    root = tmp_path_factory.mktemp("served")
    _use_drafts_dir(mp, root)
    catalog_mod.set_catalog(None)
    health = httpx.get(get_settings().base_url.rstrip("/") + "/health", timeout=5)
    assert health.status_code == 200, f"host API not healthy: {health.status_code}"
    try:
        with _Server() as srv:
            srv.drafts_root = root  # type: ignore[attr-defined]
            yield srv
    finally:
        catalog_mod.set_catalog(None)
        mp.undo()
        get_settings.cache_clear()
        drafts.reset_stores()


@pytest.fixture
def drafts_dir(server: _Server, tmp_path: Path) -> Iterator[Path]:
    """A fresh, empty drafts directory every tool call of this test writes to."""
    mp = pytest.MonkeyPatch()
    path = tmp_path / "drafts"
    _use_drafts_dir(mp, path)
    try:
        yield path
    finally:
        _use_drafts_dir(mp, server.drafts_root)  # type: ignore[attr-defined]
        mp.undo()


class _Mcp:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def call(self, tool: str, /, **args: Any) -> dict[str, Any]:
        result = await self.client.call_tool(tool, args)
        assert result.structured_content is not None, f"{tool} returned no structured content"
        return result.structured_content

    async def error(self, tool: str, /, **args: Any) -> str:
        result = await self.client.call_tool(tool, args, raise_on_error=False)
        assert result.is_error, f"{tool} succeeded: {result.structured_content}"
        return " ".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
async def mcp(server: _Server) -> Any:
    async with Client(f"{server.base_url}/mcp", timeout=120) as client:
        yield _Mcp(client)


def _file(root: Path, draft_id: str) -> Path:
    return root / "screens" / f"{draft_id}.screen.json"


def _written(root: Path) -> set[str]:
    screens = root / "screens"
    return {str(p.relative_to(screens))[: -len(".screen.json")] for p in screens.rglob("*.screen.json")}


def _snapshot(root: Path, ids: list[str]) -> dict[str, tuple[bytes, int]]:
    return {i: (_file(root, i).read_bytes(), _file(root, i).stat().st_mtime_ns) for i in ids}


def _pointer(doc: Any, path: str) -> Any:
    for part in path.lstrip("/").split("/"):
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


def _node(nid: str, ntype: str, props: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    node = {"id": nid, "type": ntype, "props": props or {}, "geometry": {"x": 0, "y": 0, "width": 80, "height": 20}}
    node.update(extra)
    return node


def _doc(*children: dict[str, Any], **top: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": "t", "source": {"type": "native"}, "size": {"width": 400, "height": 300}},
        "macros": [],
        "root": _node("root", "group", children=list(children)),
    }
    doc["root"]["geometry"] = {"x": 0, "y": 0, "width": 400, "height": 300}
    doc.update(top)
    return doc


LABEL = _node("title", "text-label", {"text": "RF status"})


# --- registry and catalog tools ----------------------------------------------------


async def test_every_tool_is_registered(mcp: _Mcp) -> None:
    tools = await mcp.client.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_catalog_tools(mcp: _Mcp) -> None:
    widgets = await mcp.call("list_widgets")
    types = {w["type"] for w in widgets["widgets"]}
    assert widgets["total"] == len(widgets["widgets"])
    assert {"text-label", "pv-label", "pv-button", "unknown-widget"} <= types

    label = await mcp.call("get_widget", type="text-label")
    assert label["type"] == "text-label"
    assert "text" in json.dumps(label)
    assert "unknown widget type" in await mcp.error("get_widget", type="no-such-widget")

    schema = await mcp.call("get_schema")
    assert "Rule" in (schema.get("$defs") or schema.get("definitions") or {})


# --- read tools, SC5 ------------------------------------------------------------------


async def test_read_pv_answers_without_writing(mcp: _Mcp) -> None:
    result = await mcp.call("read_pv", name="NO:SUCH:PV", describe=False)
    assert result["value"]["pv"] == "NO:SUCH:PV"
    assert set(result["value"]) >= {"value", "connected", "severity"}


async def test_sc5_skipped_names_raccoon_adapter_misconfigured(mcp: _Mcp, drafts_dir: Path) -> None:
    doc = _doc(_node("readback", "pv-label", {"pv": "SR:C01:BPM1:X"}))
    checked = await mcp.call("check", screen=doc)
    assert checked["skipped"] == [{"check": "raccoon", "reason": "adapter_misconfigured"}]

    searched = await mcp.call("search_pvs", pattern="SR:C01:*")
    assert searched["names"] == []
    assert searched["skipped"] == checked["skipped"]


async def test_sc5_check_speed_fixture_under_two_seconds(mcp: _Mcp, drafts_dir: Path) -> None:
    doc = json.loads(SPEED_FIXTURE.read_text())
    await mcp.call("check", screen=_doc(LABEL))  # warm: catalog loaded, session open
    start = time.perf_counter()
    checked = await mcp.call("check", screen=doc)
    elapsed = time.perf_counter() - start
    assert checked["schema"] == [] and checked["semantic"] == []
    assert [s["reason"] for s in checked["skipped"]] == ["adapter_misconfigured"]
    assert elapsed < 2.0, f"check took {elapsed:.3f} s (budget 2 s)"


# --- SC3a ------------------------------------------------------------------------------


async def test_sc3a_btsview_queue(mcp: _Mcp, drafts_dir: Path) -> None:
    result = await mcp.call("convert_legacy", **_bts_args())
    assert sorted(d["draftId"] for d in result["drafts"]) == BTS_DRAFTS
    assert _written(drafts_dir) == set(BTS_DRAFTS)
    for draft_id, title in BTS_TITLES.items():
        assert json.loads(_file(drafts_dir, draft_id).read_text())["metadata"]["title"] == title

    # The six images the panels reference are copied beside their drafts; none fails.
    assets = result["assets"]
    assert len(assets) == 6, assets
    assert {a["status"] for a in assets} <= {"written", "reused"}, assets

    queue = result["queue"]
    codes = Counter(item["code"] for item in queue)
    assert codes == Counter({"dropped_prop": 12, "warning": 2, "unresolved_ref": 3}), codes
    assert codes["unknown_widget"] == 0

    warnings = Counter(i["draftId"] for i in queue if i["code"] == "warning")
    assert warnings == Counter({"bts/BTSView": 1, "bts/embedded/_tv": 1})

    unresolved = [i for i in queue if i["code"] == "unresolved_ref"]
    assert {i["draftId"] for i in unresolved} == {"common/KnobPanel"}
    assert {i["reason"] for i in unresolved} == {"missing"}
    knob = json.loads(_file(drafts_dir, "common/KnobPanel").read_text())
    targets = Counter(resolve_ref(_pointer(knob, i["path"]), "common")[0] for i in unresolved)
    assert targets == Counter({"common/embedded/_KnobPanel": 2, "common/KnobExpert": 1})

    dropped = [i for i in queue if i["code"] == "dropped_prop"]
    per_draft = Counter(i["draftId"] for i in dropped)
    assert per_draft == Counter({"bts/BTSView": 8, "bts/embedded/_KnobDevice": 2, "common/KnobPanel": 2})

    def props(draft_id: str) -> set[str]:
        return {p for i in dropped if i["draftId"] == draft_id for p in i["props"]}

    assert props("bts/BTSView") == {"resize", "tooltip", "border_alarm_sensitive", "backgroundColor"}
    assert props("bts/embedded/_KnobDevice") == {"tooltip"}
    assert props("bts/embedded/_bpm") == set()
    assert props("common/KnobPanel") == {"resize"}
    assert "style" not in {p for i in dropped for p in i["props"]}

    # Clearing the Action Button_7 execute note stops check reporting it.
    [note] = [i for i in queue if i["code"] == "warning" and i["draftId"] == BTS_ID]
    patched = await mcp.call(
        "patch_node", screen_id=BTS_ID, node_id=note["nodeId"], base_rev=result["rev"], set={"warnings": []}
    )
    assert patched.get("refused") is not True, patched
    checked = await mcp.call("check", draft_id=BTS_ID)
    assert [f for f in checked["semantic"] if f["code"] == "warning" and f["nodeId"] == note["nodeId"]] == []

    # The remaining read/write tools on the converted drafts.
    listed = await mcp.call("list_drafts")
    assert {d["draftId"]: d["title"] for d in listed["drafts"]} == BTS_TITLES
    got = await mcp.call("get_screen", id=BTS_ID)
    assert got["rev"] == patched["rev"] and got["screen"]["id"] == BTS_ID
    tv = await mcp.call("get_screen", id="bts/embedded/_tv")
    deleted = await mcp.call("delete_draft", id="bts/embedded/_tv", base_rev=tv["rev"])
    assert deleted == {"draftId": "bts/embedded/_tv", "deleted": True}
    assert not _file(drafts_dir, "bts/embedded/_tv").exists()


# --- SC3c ------------------------------------------------------------------------------


async def test_sc3c_cavity1water_tanks_drain_to_zero(mcp: _Mcp, drafts_dir: Path) -> None:
    draft_id = "srrf/Cavity1Water"
    result = await mcp.call(
        "convert_legacy", id=draft_id, path=str(ACC / "sr" / "srrf" / "Cavity1Water.bob"), kind="bob"
    )
    unknown = [i for i in result["queue"] if i["code"] == "unknown_widget"]
    assert len(unknown) == 7
    assert {i["originalClass"] for i in unknown} == {"tank"}

    rev = result["rev"]
    for n, item in enumerate(unknown):
        screen = (await mcp.call("get_screen", id=draft_id))["screen"]
        node = _find(screen["root"], item["nodeId"])
        parent = _parent_of(screen["root"], item["nodeId"])
        removed = await mcp.call("remove_node", screen_id=draft_id, node_id=item["nodeId"], base_rev=rev)
        assert removed.get("refused") is not True, removed
        replacement = {
            "id": f"tank_replacement_{n}",
            "type": "pv-label",
            "props": {"text": "tank"},
            "geometry": copy.deepcopy(node["geometry"]),
        }
        added = await mcp.call(
            "add_node", screen_id=draft_id, parent_id=parent["id"], node=replacement, base_rev=removed["rev"]
        )
        assert added.get("refused") is not True, added
        rev = added["rev"]

    checked = await mcp.call("check", draft_id=draft_id)
    assert [f for f in checked["semantic"] if f["code"] == "unknown_widget"] == []


async def test_sc3c_patch_accepted_on_one_of_two_unknown_widgets(mcp: _Mcp, drafts_dir: Path) -> None:
    # The fixture's plain node is a unit-test ``label``; the live catalog calls it ``text-label``.
    doc = json.loads(TWO_UNKNOWN.read_text().replace('"type": "label"', '"type": "text-label"'))
    created = await mcp.call("new_screen", id="synthetic/two-unknown", screen=doc)
    assert created.get("refused") is not True, created
    assert sum(w["code"] == "unknown_widget" for w in created["warnings"]) == 2
    patched = await mcp.call(
        "patch_node",
        screen_id="synthetic/two-unknown",
        node_id="tank_1",
        base_rev=created["rev"],
        set={"geometry": {"x": 20}},
    )
    assert patched.get("refused") is not True, patched
    assert patched["rev"] != created["rev"]


def _find(node: dict[str, Any], node_id: str) -> dict[str, Any]:
    if node.get("id") == node_id:
        return node
    for child in node.get("children") or []:
        try:
            return _find(child, node_id)
        except KeyError:
            continue
    raise KeyError(node_id)


def _parent_of(node: dict[str, Any], node_id: str) -> dict[str, Any]:
    for child in node.get("children") or []:
        if child.get("id") == node_id:
            return node
        try:
            return _parent_of(child, node_id)
        except KeyError:
            continue
    raise KeyError(node_id)


# --- SC6 ---------------------------------------------------------------------------------


async def test_sc6_invalid_document_writes_nothing_and_names_nodes(mcp: _Mcp, drafts_dir: Path) -> None:
    bad = _doc(_node("broken", "text-label", {"text": "x"}, geometry="not-an-object"))
    refused = await mcp.call("new_screen", id="sc6/invalid", screen=bad)
    assert refused["refused"] is True and refused["reason"] == "check_errors"
    assert refused["errors"], refused
    assert all("nodeId" in e and "path" in e for e in refused["errors"])
    assert "broken" in {e["nodeId"] for e in refused["errors"]}
    assert not (drafts_dir / "screens").exists() or _written(drafts_dir) == set()


@pytest.mark.parametrize(
    ("node_id", "code", "doc"),
    [
        (
            "twin",
            "duplicate_node_id",
            _doc(_node("twin", "text-label", {"text": "a"}), _node("twin", "text-label", {"text": "b"})),
        ),
        (
            "web",
            "open_webpage",
            _doc(
                _node(
                    "web",
                    "regular-button",
                    {"label": "go", "actions": [{"type": "open_webpage", "url": "javascript:alert(1)"}]},
                )
            ),
        ),
        (None, "macro_default", _doc(LABEL, macros=[{"name": "P"}])),
        (
            "coloured",
            "unresolved_formula",
            _doc(
                _node(
                    "coloured",
                    "text-label",
                    {"text": "x"},
                    rules=[
                        {
                            "id": "r1",
                            "name": "colour",
                            "targetProperty": "foregroundColor",
                            "default": "#000000",
                            "formula": "fox://missing",
                        }
                    ],
                )
            ),
        ),
    ],
    ids=["duplicate-id", "javascript-url", "macro-default", "fox-missing"],
)
async def test_sc6_refusals_name_the_node_and_code(
    mcp: _Mcp, drafts_dir: Path, node_id: str | None, code: str, doc: dict[str, Any]
) -> None:
    refused = await mcp.call("new_screen", id="sc6/refused", screen=doc)
    assert refused["refused"] is True and refused["reason"] == "check_errors", refused
    assert (node_id, code) in {(e["nodeId"], e["code"]) for e in refused["errors"]}
    assert not _file(drafts_dir, "sc6/refused").exists()


async def test_sc6_pv_button_warns_inert_in_osprey(mcp: _Mcp, drafts_dir: Path) -> None:
    doc = _doc(_node("press", "pv-button", {"pv": "SR:C01:BPM1:X", "label": "press"}))
    created = await mcp.call("new_screen", id="sc6/button", screen=doc)
    assert created.get("refused") is not True, created
    assert ("press", "inert_in_osprey") in {(w["nodeId"], w["code"]) for w in created["warnings"]}


def _raw_get(base_url: str, path: str) -> int:
    """GET ``path`` byte-for-byte (no client-side dot-segment normalization)."""
    host, port = base_url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


async def test_sc6_draft_traversal_is_404(server: _Server) -> None:
    assert _raw_get(server.base_url, "/drafts/../x") == 404
    assert _raw_get(server.base_url, "/drafts/%2e%2e/x") == 404


async def test_sc6_nested_ref_base(mcp: _Mcp, drafts_dir: Path) -> None:
    shutil.copytree(REFS_DRAFTS, drafts_dir)
    checked = await mcp.call("check", draft_id="a/top")
    # The child's ref resolves beside the child (a/sub/x), so the retired code and its stand-ins are absent.
    assert [f for f in checked["semantic"] if f["code"].endswith(("_ref", "_ref_base"))] == []


async def test_sc6_lint_with_a_mocked_transport(server: _Server) -> None:
    catalog = await catalog_mod.ensure_catalog()
    undocumented = next(t for t in catalog.types if catalog.entries[t].undocumented)
    doc = _doc(
        _node("undoc", undocumented),
        _node("alien", "no-such-widget"),
        _node("ghost", "pv-label", {"pv": "NO:PE"}),
        _node("real", "pv-label", {"pv": "SR:C01:BPM1:X"}),
    )

    def raccoon(request: httpx.Request) -> httpx.Response:
        assert request.url.path == RACCOON_VALIDATE_PATH
        return httpx.Response(200, json={"known": ["SR:C01:BPM1:X"], "unknown": ["NO:PE"]})

    async with httpx.AsyncClient(base_url="http://canopy.test", transport=httpx.MockTransport(raccoon)) as client:
        result = await check_lint(doc, catalog, client=client)
    got = {(f.node_id, f.severity) for f in result.findings}
    assert ("undoc", "warning") in got
    assert ("alien", "error") in got
    assert ("ghost", "warning") in got
    assert not any(f.node_id == "real" for f in result.findings)
    assert result.skipped == []


# --- SC7 ---------------------------------------------------------------------------------


async def test_sc7_existing_id_and_stale_rev_leave_the_draft_intact(mcp: _Mcp, drafts_dir: Path) -> None:
    created = await mcp.call("new_screen", id="sc7/panel", screen=_doc(LABEL))
    before = _file(drafts_dir, "sc7/panel").read_bytes()

    again = await mcp.call("new_screen", id="sc7/panel", screen=_doc(_node("other", "text-label", {"text": "y"})))
    assert again["refused"] is True and again["reason"] == "exists"
    stale = await mcp.call(
        "patch_node", screen_id="sc7/panel", node_id="title", base_rev="0" * 64, set={"props": {"text": "new"}}
    )
    assert stale["refused"] is True and stale["reason"] == "stale_rev"
    assert stale["currentRev"] == created["rev"]
    assert _file(drafts_dir, "sc7/panel").read_bytes() == before


async def test_sc7_double_conversion_then_overwrite_refusal(mcp: _Mcp, drafts_dir: Path) -> None:
    assert not drafts_dir.exists() or _written(drafts_dir) == set()
    first = await mcp.call("convert_legacy", **_bts_args())
    revs = {d["draftId"]: d["rev"] for d in first["drafts"]}
    assert sorted(revs) == BTS_DRAFTS
    before = _snapshot(drafts_dir, BTS_DRAFTS)

    second = await mcp.call("convert_legacy", **_bts_args())
    assert {d["draftId"]: d["rev"] for d in second["drafts"]} == revs
    assert _snapshot(drafts_dir, BTS_DRAFTS) == before

    note = next(i for i in first["queue"] if i["code"] == "warning" and i["draftId"] == BTS_ID)
    patched = await mcp.call(
        "patch_node", screen_id=BTS_ID, node_id=note["nodeId"], base_rev=revs[BTS_ID], set={"warnings": []}
    )
    assert patched.get("refused") is not True, patched
    edited = {i: _file(drafts_dir, i).read_bytes() for i in BTS_DRAFTS}

    refused = await mcp.call("convert_legacy", **_bts_args(), overwrite=True, base_revs={})
    assert refused["refused"] is True and refused["reason"] == "conflict"
    assert refused["conflicts"] == [BTS_ID]
    assert {i: _file(drafts_dir, i).read_bytes() for i in BTS_DRAFTS} == edited


async def test_sc7_one_unresolved_ref_per_reason_and_the_call_succeeds(
    mcp: _Mcp, drafts_dir: Path, tmp_path: Path
) -> None:
    sources = mocks.write_sources(tmp_path / "src")
    result = await mcp.call(
        "convert_legacy",
        id="a/parent",
        path=str(sources["parent"]),
        kind="bob",
        siblings={"embedded/_warn.bob": str(sources["warn"]), "embedded/_drop.bob": str(sources["drop"])},
    )
    assert result.get("refused") is not True, result
    reasons = Counter(
        i["reason"] for i in result["queue"] if i["code"] == "unresolved_ref" and i["draftId"] == "a/parent"
    )
    assert reasons == Counter({"absolute": 1, "above_root": 1, "invalid_id": 1, "missing": 1})
    assert _written(drafts_dir) == {"a/parent", "a/embedded/_warn", "a/embedded/_drop", "a/def"}


@pytest.fixture
def recorded_conversions(server: _Server, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request ``convert_legacy`` sends, answered 500 by a mock transport."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="the mock converter must not be called")

    def make_client(settings: Any = None, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return httpx.AsyncClient(base_url="http://canopy.test", transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(convert, "make_client", make_client)
    return seen


async def test_sc7_relative_path_refuses_with_zero_conversion_calls(
    mcp: _Mcp, drafts_dir: Path, recorded_conversions: list[httpx.Request]
) -> None:
    refused = await mcp.call("convert_legacy", id=BTS_ID, path="bts/BTSView.bob", kind="bob")
    assert refused["refused"] is True and refused["keys"] == ["path"]

    args = _bts_args()
    args["siblings"]["embedded/_bpm.bob"] = "gtb/common/embedded/_bpm.bob"
    refused = await mcp.call("convert_legacy", **args)
    assert refused["refused"] is True and refused["keys"] == ["embedded/_bpm.bob"]

    assert recorded_conversions == []
    assert not drafts_dir.exists() or _written(drafts_dir) == set()


# --- SC8 (direct halves) ------------------------------------------------------------------


async def test_sc8_pv_write_and_restore_answer_405(server: _Server) -> None:
    async with httpx.AsyncClient(base_url=server.base_url, timeout=10) as client:
        put = await client.put("/hare/v1/pvs/X", json={"value": 1})
        restore = await client.post("/squirrel/x/restore", json={})
    assert put.status_code == 405
    assert restore.status_code == 405


# --- screenshot ------------------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("CANOPY_VIEWER_DIST"), reason="CANOPY_VIEWER_DIST not set")
async def test_screenshot_renders_a_draft_in_the_real_viewer(mcp: _Mcp, server: _Server) -> None:
    created = await mcp.call("new_screen", id="live/shot", screen=_doc(LABEL))
    assert created.get("refused") is not True, created
    try:
        start = time.perf_counter()
        result = await mcp.client.call_tool("screenshot", {"id": "live/shot"})
        elapsed = time.perf_counter() - start
        [image] = [c for c in result.content if isinstance(c, ImageContent)]
        assert image.mimeType == "image/png"
        info = result.structured_content
        assert info["width"] > 0 and info["height"] > 0 and info["pv"] in {"connected", "disconnected"}
        assert elapsed < 5.0, f"screenshot took {elapsed:.3f} s (budget 5 s)"
    finally:
        await mcp.call("delete_draft", id="live/shot", base_rev=created["rev"])
