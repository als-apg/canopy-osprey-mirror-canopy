"""Ref pass: the SC6 nested-base case, one fixture per unresolved reason, and the resolution rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from canopy_mcp.check.refs import RefFinding, check_refs, normalize, resolve_ref
from canopy_mcp.store import DraftStore, InvalidDraftId

FIXTURE_DRAFTS = Path(__file__).parent / "fixtures" / "refs" / "drafts"


@pytest.fixture
def fixture_store() -> DraftStore:
    return DraftStore(FIXTURE_DRAFTS)


@pytest.fixture
def store(tmp_path: Path) -> DraftStore:
    return DraftStore(tmp_path / "drafts")


def _node(nid: str, ntype: str = "label", **props: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "props": props, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}


def _embed(nid: str, file: Any) -> dict[str, Any]:
    return _node(nid, "embedded-display", file=file)


def _doc(*children: dict[str, Any]) -> dict[str, Any]:
    root = _node("w-root", "group")
    root["children"] = list(children)
    return {"schemaVersion": "1.0", "kind": "screen", "id": "demo", "root": root}


def _put(store: DraftStore, draft_id: str, *children: dict[str, Any]) -> None:
    path = store.path_for(draft_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_doc(*children)))


def _load(draft_id: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DRAFTS / "screens" / f"{draft_id}.screen.json").read_text())


# --- SC6 -------------------------------------------------------------------


def test_sc6_nested_ref_base(fixture_store: DraftStore) -> None:
    """a/top embeds sub/child; child's x.screen.json exists at a/sub/x but not at a/x."""
    findings = check_refs(_load("a/top"), fixture_store, "a/top")
    assert [f.to_dict() for f in findings] == [
        {
            "nodeId": "child-embed",
            "code": "nested_ref_base",
            "path": "/root/children/0/props/file",
            "severity": "warning",
            "message": findings[0].message,
            "docId": "a/sub/child",
        }
    ]
    assert "a/sub/x" in findings[0].message


def test_checking_the_child_itself_resolves_in_its_own_directory(fixture_store: DraftStore) -> None:
    assert check_refs(_load("a/sub/child"), fixture_store, "a/sub/child") == []


# --- one fixture per reason ------------------------------------------------


def test_one_unresolved_ref_per_reason(fixture_store: DraftStore) -> None:
    findings = check_refs(_load("a/reasons"), fixture_store, "a/reasons")
    got = {(f.node_id, f.code, f.path, f.severity, getattr(f, "reason", None), f.doc_id) for f in findings}
    assert got == {
        ("r-absolute", "unresolved_ref", "/root/children/0/props/file", "warning", "absolute", None),
        ("r-above", "unresolved_ref", "/root/children/1/props/file", "warning", "above_root", None),
        ("r-invalid", "unresolved_ref", "/root/children/2/props/file", "warning", "invalid_id", None),
        ("r-missing", "unresolved_ref", "/root/children/3/props/actions/0/file", "warning", "missing", None),
    }
    assert len(findings) == 4
    for f in findings:
        assert isinstance(f, RefFinding)
        assert f.to_dict()["reason"] == f.reason
        assert "docId" not in f.to_dict()


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["/abs/x.screen.json", "~/x.screen.json", "~user/x.screen.json", "file:x.screen.json", "https://h/x.screen.json"],
)
def test_raw_absolute_refs(ref: str) -> None:
    assert resolve_ref(ref, "a") == (None, "absolute")


def test_absolute_is_decided_before_normalization() -> None:
    # "./" in front does not hide a scheme; but a scheme-looking segment after "./" is relative.
    assert resolve_ref("http:x.screen.json", "") == (None, "absolute")
    assert resolve_ref("./sub/x.screen.json", "a") == ("a/sub/x", None)


@pytest.mark.parametrize(
    ("ref", "base", "expected"),
    [
        ("x.screen.json", "a", ("a/x", None)),
        ("x.screen.json", "", ("x", None)),
        ("../x.screen.json", "a/b", ("a/x", None)),
        ("../x.screen.json", "a", ("x", None)),
        ("../x.screen.json", "", (None, "above_root")),
        ("../../x.screen.json", "a", (None, "above_root")),
        ("sub/../../x.screen.json", "a", ("x", None)),
        ("sub/../../../x.screen.json", "a", (None, "above_root")),
        ("./sub//x.screen.json", "a", ("a/sub/x", None)),
        ("x", "a", (None, "invalid_id")),
        ("x.bob", "a", (None, "invalid_id")),
        ("bad name.screen.json", "a", (None, "invalid_id")),
        (".screen.json", "a", (None, "invalid_id")),
        ("", "a", (None, "invalid_id")),
        ("..", "a", (None, "invalid_id")),
    ],
)
def test_resolve_ref(ref: str, base: str, expected: tuple[str | None, str | None]) -> None:
    assert resolve_ref(ref, base) == expected


def test_normalize_keeps_leading_parent_segments() -> None:
    assert normalize("../a/./b/../c") == ["..", "a", "c"]
    assert normalize("a/../../b") == ["..", "b"]


# --- resolution base -------------------------------------------------------


def test_no_draft_id_resolves_against_store_root(store: DraftStore) -> None:
    _put(store, "x")
    assert check_refs(_doc(_embed("e", "x.screen.json")), store) == []
    [f] = check_refs(_doc(_embed("e", "../x.screen.json")), store)
    assert (f.code, f.reason) == ("unresolved_ref", "above_root")


def test_unsaved_document_resolves_in_its_draft_ids_directory(store: DraftStore) -> None:
    """check(screen, draft_id): the id need not be stored; its directory is the base."""
    _put(store, "p/x")
    assert check_refs(_doc(_embed("e", "x.screen.json")), store, "p/new") == []
    [f] = check_refs(_doc(_embed("e", "x.screen.json")), store, "q/new")
    assert (f.code, f.reason, f.node_id) == ("unresolved_ref", "missing", "e")


def test_invalid_draft_id_argument_raises(store: DraftStore) -> None:
    with pytest.raises(InvalidDraftId):
        check_refs(_doc(), store, "../x")


# --- embedded documents ----------------------------------------------------


def test_embedded_doc_contributes_only_ref_codes(store: DraftStore) -> None:
    # The child has a problem the semantic pass would report; refs must not collect it.
    _put(store, "c", _node("dup"), _node("dup", "pv-button"))
    assert check_refs(_doc(_embed("e", "c.screen.json")), store, "top") == []


def test_nested_refs_use_the_top_documents_base(store: DraftStore) -> None:
    """Same directory for top and child: both rules agree, a missing ref is unresolved_ref with docId."""
    _put(store, "a/child", _embed("grand", "gone.screen.json"), _embed("ok", "sib.screen.json"))
    _put(store, "a/sib")
    [f] = check_refs(_doc(_embed("e", "child.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id) == ("grand", "unresolved_ref", "missing", "a/child")
    assert f.path == "/root/children/0/props/file"


def test_nested_open_display_ref_is_checked(store: DraftStore) -> None:
    _put(store, "a/sub/child", _node("btn", "pv-button", actions=[{"type": "open_display", "file": "x.screen.json"}]))
    _put(store, "a/sub/x")
    [f] = check_refs(_doc(_embed("e", "sub/child.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.doc_id, f.path) == (
        "btn",
        "nested_ref_base",
        "a/sub/child",
        "/root/children/0/props/actions/0/file",
    )


def test_open_display_targets_are_not_followed(store: DraftStore) -> None:
    _put(store, "a/other", _embed("deep", "gone.screen.json"))
    doc = _doc(_node("btn", "pv-button", actions=[{"type": "open_display", "file": "other.screen.json"}]))
    assert check_refs(doc, store, "a/top") == []


def test_grandchild_follows_top_rule_and_reports_doc_id(store: DraftStore) -> None:
    _put(store, "a/c1", _embed("to-c2", "c2.screen.json"))
    _put(store, "a/c2", _embed("broken", "/abs.screen.json"))
    [f] = check_refs(_doc(_embed("e", "c1.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id) == ("broken", "unresolved_ref", "absolute", "a/c2")


def test_embed_cycles_terminate(store: DraftStore) -> None:
    _put(store, "a/top", _embed("t", "c.screen.json"))
    _put(store, "a/c", _embed("back", "top.screen.json"), _embed("self", "c.screen.json"))
    doc = json.loads(store.path_for("a/top").read_text())
    assert check_refs(doc, store, "a/top") == []


def test_unreadable_embedded_doc_is_skipped(store: DraftStore) -> None:
    path = store.path_for("a/c")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert check_refs(_doc(_embed("e", "c.screen.json")), store, "a/top") == []


def test_malformed_input_is_tolerated(store: DraftStore) -> None:
    assert check_refs(None, store) == []
    assert check_refs({"root": "nope"}, store) == []
    doc = _doc(
        _embed("e1", 42),
        _node("e2", "embedded-display"),
        _node("b", "pv-button", actions=["x", {"type": "open_display"}, {"type": "open_display", "file": None}]),
        {"type": "embedded-display", "props": {"file": "gone.screen.json"}},
    )
    [f] = check_refs(doc, store, "t")
    assert (f.node_id, f.reason, f.path) == (None, "missing", "/root/children/3/props/file")


def test_file_prop_on_other_node_types_is_ignored(store: DraftStore) -> None:
    assert check_refs(_doc(_node("img", "image", file="gone.screen.json")), store) == []
