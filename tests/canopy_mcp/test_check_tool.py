"""The ``check`` tool: argument rules and composition of the four passes."""

import sys
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock
from fastmcp.exceptions import ToolError

from canopy_mcp.store import DraftStore
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.catalog import set_catalog, build_catalog
from canopy_mcp.check.lint import RACCOON_VALIDATE_PATH
from canopy_mcp.tools.check import CheckResult, check, run_check
from canopy_mcp.check.semantic import iter_nodes
from tests.canopy_mcp.synthetic_spec import write_spec

BASE = "http://canopy.test"
RACCOON = BASE + RACCOON_VALIDATE_PATH
FIXTURES = Path(__file__).parent / "fixtures"
REFS_DRAFTS = FIXTURES / "refs" / "drafts"
SPEED_FIXTURE = FIXTURES / "speed-100-50-20.screen.json"
GENERATOR = FIXTURES / "gen_speed_fixture.py"


@pytest.fixture(autouse=True)
def staged_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A synthetic staged spec: these tests compose all four passes (the spec-absent path is test_check_schema's)."""
    spec = write_spec(tmp_path / "spec")
    monkeypatch.setenv("CANOPY_SPEC_DIR", str(spec))
    return spec


LABEL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {"pv": {"type": "string"}, "precision": {"type": "integer", "minimum": 0}},
    "required": ["pv"],
}


def _catalog():
    ir_map = {
        "pv-label": {"consumes": ["pv", "precision"], "verbatim": False},
        "group": {"consumes": [], "verbatim": False},
        "embedded-display": {"consumes": ["file"], "verbatim": False},
    }
    return build_catalog(ir_map, {"pv-label": LABEL_SCHEMA})


def _node(nid, ntype, props=None, children=None):
    node = {"id": nid, "type": ntype, "props": props or {}, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}
    if children is not None:
        node["children"] = children
    return node


def _doc(*children, **top):
    doc = {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": "t", "source": {"type": "native"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": _node("root", "group", children=list(children)),
    }
    doc.update(top)
    return doc


@pytest.fixture
def client():
    return make_client(Settings(base_url=BASE))


@pytest.fixture
def store(tmp_path):
    shutil.copytree(REFS_DRAFTS, tmp_path / "drafts")
    return DraftStore(tmp_path / "drafts")


def _keys(findings):
    return {(f["nodeId"], f["code"], f["path"]) for f in findings}


# --- argument rules -----------------------------------------------------------


async def test_neither_argument_is_an_error(store):
    with pytest.raises(ToolError, match="screen document or a draft_id"):
        await run_check(store=store, catalog=_catalog())


async def test_tool_with_neither_argument_is_an_error():
    with pytest.raises(ToolError, match="screen document or a draft_id"):
        await check()


async def test_missing_draft_is_an_error(store):
    with pytest.raises(ToolError, match="no draft stored"):
        await run_check(draft_id="nope/none", store=store, catalog=_catalog())


@pytest.mark.parametrize("bad", ["../x", "a/", "a b"])
async def test_invalid_draft_id_is_an_error(store, bad):
    with pytest.raises(ToolError, match="invalid draft id"):
        await run_check(_doc(), bad, store=store, catalog=_catalog())


# --- composition ---------------------------------------------------------------


async def test_each_pass_lands_in_its_own_bucket(httpx_mock: HTTPXMock, client, store):
    httpx_mock.add_response(method="POST", url=RACCOON, json={"known": [], "unknown": ["NO:SUCH:PV"]})
    doc = _doc(
        _node("lbl", "pv-label", {"pv": "NO:SUCH:PV", "precision": -1}),
        _node("dup", "group"),
        _node("dup", "group"),
        _node("emb", "embedded-display", {"file": "missing.screen.json"}),
    )
    doc["root"]["children"][1]["geometry"] = {"x": 0}  # schema: width/height/y missing

    result = (await run_check(doc, store=store, catalog=_catalog(), client=client)).to_dict()

    assert set(result) == {"schema", "semantic", "lint", "skipped"}
    assert {f["code"] for f in result["schema"]} == {"schema"}
    assert all(f["path"].startswith("/root/children/1/geometry") for f in result["schema"])
    assert {("dup", "duplicate_node_id"), ("emb", "unresolved_ref")} <= {
        (f["nodeId"], f["code"]) for f in result["semantic"]
    }
    assert ("lbl", "invalid_prop", "/root/children/0/props/precision") in _keys(result["lint"])
    assert ("lbl", "unknown_pv", "/root/children/0/props/pv") in _keys(result["lint"])
    assert result["skipped"] == []
    for bucket in ("schema", "semantic", "lint"):
        for finding in result[bucket]:
            assert {"nodeId", "code", "path", "severity", "message"} <= set(finding)


async def test_clean_document_has_no_findings(httpx_mock: HTTPXMock, client, store):
    httpx_mock.add_response(method="POST", url=RACCOON, json={"known": ["A:B"], "unknown": []})
    result = await run_check(
        _doc(_node("lbl", "pv-label", {"pv": "A:B"})), store=store, catalog=_catalog(), client=client
    )
    assert result.to_dict() == {"schema": [], "semantic": [], "lint": [], "skipped": []}
    assert result.findings == [] and result.errors == []


async def test_a_plot_curve_without_its_channel_key_is_an_error_a_write_refuses_on(
    httpx_mock: HTTPXMock, client, store
):
    """The semantic pass refuses the curve no matter what the staged spec says of props."""
    # ``channel`` is a key the renderer subscribes to, so the PV pass asks about A:B.
    httpx_mock.add_response(method="POST", url=RACCOON, json={"known": ["A:B"], "unknown": []})
    catalog = build_catalog(
        {"group": {"consumes": [], "verbatim": False}, "waveform-plot": {"consumes": ["curves"], "verbatim": False}}
    )
    doc = _doc(_node("plt", "waveform-plot", {"curves": [{"channel": "A:B"}]}))

    result = await run_check(doc, store=store, catalog=catalog, client=client)

    assert _keys(result.to_dict()["semantic"]) == {
        ("plt", "curve_channel", "/root/children/0/props/curves/0/y_channel")
    }
    assert [f.code for f in result.errors] == ["curve_channel"]
    assert result.to_dict()["schema"] == [] and result.to_dict()["skipped"] == []


async def test_raccoon_outage_is_reported_in_skipped(httpx_mock: HTTPXMock, client, store):
    httpx_mock.add_response(method="POST", url=RACCOON, status_code=500, json={"code": "adapter_misconfigured"})
    result = await run_check(
        _doc(_node("lbl", "pv-label", {"pv": "A:B"})), store=store, catalog=_catalog(), client=client
    )
    assert result.skipped == [{"check": "raccoon", "reason": "adapter_misconfigured"}]
    assert result.lint == []


async def test_stored_draft_is_read_and_refs_carry_doc_id(store, client):
    """draft_id alone: the stored draft is checked; a finding in an embedded screen names its document."""
    result = (await run_check(draft_id="a/top", store=store, catalog=_catalog(), client=client)).to_dict()
    # a/sub/child's ref resolves beside a/sub/child, so the stored a/top has no ref finding
    assert [f for f in result["semantic"] if f["code"].endswith("_ref")] == []
    outer = _doc(_node("emb", "embedded-display", {"file": "reasons.screen.json"}))
    result = (await run_check(outer, "a/outer", store=store, catalog=_catalog(), client=client)).to_dict()
    nested = [f for f in result["semantic"] if f["code"] == "unresolved_ref"]
    assert nested and {f["docId"] for f in nested} == {"a/reasons"}
    assert all("docId" not in f for f in result["schema"] + result["lint"])


async def test_document_with_draft_id_resolves_refs_there_without_reading_it(store, client):
    """A given document plus draft_id: refs resolve in a/, and the stored a/top is not what is checked."""
    doc = _doc(_node("emb", "embedded-display", {"file": "sub/child.screen.json"}))
    at_root = await run_check(doc, store=store, catalog=_catalog(), client=client)
    in_a = await run_check(doc, "a/top", store=store, catalog=_catalog(), client=client)
    assert ("emb", "unresolved_ref") in {(f.node_id, f.code) for f in at_root.semantic}
    assert ("emb", "unresolved_ref") not in {(f.node_id, f.code) for f in in_a.semantic}
    # the stored a/top's own node ids never appear: the given document was checked
    assert "top-embed" not in {f.node_id for f in in_a.findings}


async def test_errors_are_the_error_severity_findings(httpx_mock: HTTPXMock, client, store):
    httpx_mock.add_response(method="POST", url=RACCOON, json={"known": [], "unknown": ["X:Y"]})
    result = await run_check(
        _doc(_node("a", "group"), _node("a", "pv-label", {"pv": "X:Y"})), store=store, catalog=_catalog(), client=client
    )
    assert {f.code for f in result.errors} == {"duplicate_node_id"}
    assert "unknown_pv" in {f.code for f in result.findings}
    assert all(f.severity == "error" for f in result.errors)


async def test_tool_uses_settings_store_and_process_catalog(httpx_mock: HTTPXMock, monkeypatch, tmp_path):
    shutil.copytree(REFS_DRAFTS, tmp_path / "drafts")
    monkeypatch.setenv("CANOPY_BASE_URL", BASE)
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(tmp_path / "drafts"))
    get_settings.cache_clear()
    set_catalog(_catalog())
    try:
        result = await check(draft_id="a/reasons")
    finally:
        set_catalog(None)
        get_settings.cache_clear()
    assert isinstance(result, dict) and set(result) == {"schema", "semantic", "lint", "skipped"}
    assert "unresolved_ref" in {f["code"] for f in result["semantic"]}
    json.dumps(result)  # plain JSON for the MCP response


def test_check_result_defaults():
    assert CheckResult().to_dict() == {"schema": [], "semantic": [], "lint": [], "skipped": []}


# --- speed fixture ---------------------------------------------------------------


def test_speed_fixture_is_reproducible():
    proc = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_speed_fixture_shape():
    doc = json.loads(SPEED_FIXTURE.read_text())
    nodes = [node for _, node in iter_nodes(doc)]
    rules = [rule for node in nodes for rule in node.get("rules", [])]
    expressions = {f["expression"] for f in doc["formulas"]}
    assert len(nodes) == 100
    assert len({node["id"] for node in nodes}) == 100
    assert len(rules) == 50
    assert len(doc["formulas"]) == 20 and len(expressions) == 20


async def test_speed_fixture_passes_offline_passes(store):
    """Schema and semantic are clean, so the live timing measures a realistic check."""
    doc = json.loads(SPEED_FIXTURE.read_text())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"known": [], "unknown": []}))
    async with httpx.AsyncClient(base_url=BASE, transport=transport) as client:
        result = await run_check(doc, store=store, catalog=_catalog(), client=client)
    assert result.schema == [] and result.semantic == []
    assert "invalid_formula" not in {f.code for f in result.lint}
