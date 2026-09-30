"""Ref pass: the SC6 nested-base case, one fixture per unresolved reason, and the resolution rules."""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

import pytest

from canopy_mcp.store import DraftStore, InvalidDraftId
from canopy_mcp.check.refs import (
    RefFinding,
    iter_refs,
    normalize,
    check_refs,
    resolve_ref,
    iter_image_refs,
    resolve_image_ref,
)

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
        assert f.to_dict()["kind"] == "screen"
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


# --- image refs -------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _image(nid: str, src: Any) -> dict[str, Any]:
    return _node(nid, "image-view", src=src)


def _image_findings(findings: list[Any]) -> list[RefFinding]:
    return [f for f in findings if isinstance(f, RefFinding) and f.kind == "image"]


def test_iter_image_refs_yields_image_view_and_pv_button_images_only() -> None:
    doc = _doc(
        _image("img", "a.png"),
        _node("btn", "pv-button", onImage="on.png", offImage="off.png"),
        _node("lbl", "label", src="nope.png"),
        _image("empty", ""),
        _image("nonstr", 3),
        _embed("e", "x.screen.json"),
    )
    assert iter_image_refs(doc) == [
        ("img", ("root", "children", 0, "props", "src"), "a.png"),
        ("btn", ("root", "children", 1, "props", "onImage"), "on.png"),
        ("btn", ("root", "children", 1, "props", "offImage"), "off.png"),
    ]
    assert [r[2] for r in iter_refs(doc)] == ["x.screen.json"]  # iter_refs untouched: screens only


@pytest.mark.parametrize(
    ("ref", "base", "expected"),
    [
        ("https://x/a.png", "a", (None, "external")),
        ("HTTPS://x/A.png", "a", (None, "external")),
        ("http://x/a.png", "a", (None, "external")),
        ("data:image/png;base64,AAAA", "a", (None, "external")),
        ("blob:abc", "a", (None, "external")),
        ("/static/a.png", "a", (None, "external")),
        ("platform:/plugin/x/icons/a.png", "a", (None, "invalid_ref")),
        ("file:a.png", "a", (None, "invalid_ref")),
        ("../../x.png", "a", (None, "above_root")),
        ("../x.png", "", (None, "above_root")),
        ("${IMG}.png", "a", (None, "invalid_ref")),
        ("~/a.png", "a", (None, "invalid_ref")),
        ("a%20b.png", "a", (None, "invalid_ref")),
        ("x.bob", "a", (None, "invalid_ref")),
        ("x.screen.json/a.png", "a", (None, "invalid_ref")),
        ("images/gone.png", "a", ("a/images/gone.png", None)),
        ("./images/../logo one.PNG", "a/b", ("a/b/logo one.PNG", None)),
        ("../x.png", "a", ("x.png", None)),
        ("x.png", "", ("x.png", None)),
    ],
)
def test_resolve_image_ref(ref: str, base: str, expected: tuple[Any, Any]) -> None:
    assert resolve_image_ref(ref, base) == expected


def test_one_image_message_per_reason_in_top_document(store: DraftStore) -> None:
    doc = _doc(
        _image("i-missing", "images/gone.png"),
        _image("i-above", "../../x.png"),
        _image("i-macro", "${IMG}.png"),
        _image("i-platform", "platform:/plugin/x/icons/a.png"),
        _image("i-https", "https://x/a.png"),
        _image("i-HTTPS", "HTTPS://x/A.png"),
        _image("i-data", "data:image/png;base64,AAAA"),
    )
    findings = check_refs(doc, store, "a/top")
    assert findings == _image_findings(findings)
    got = {f.node_id: f for f in findings}
    assert set(got) == {"i-missing", "i-above", "i-macro", "i-platform"}
    assert {nid: f.reason for nid, f in got.items()} == {
        "i-missing": "missing",
        "i-above": "above_root",
        "i-macro": "invalid_ref",
        "i-platform": "invalid_ref",
    }
    for f in findings:
        d = f.to_dict()
        assert d["code"] == "unresolved_ref"
        assert d["severity"] == "warning"
        assert d["kind"] == "image"
        assert d["reason"] == f.reason
        assert d["path"].endswith("/props/src")
        assert "docId" not in d
    assert got["i-missing"].message.startswith("image 'images/gone.png' is not in the draft store")
    assert "looked for screens/a/images/gone.png" in got["i-missing"].message
    assert "re-port with overwrite=true" in got["i-missing"].message
    assert "climbs above the drafts root (resolved against 'a/')" in got["i-above"].message
    assert "is not a valid asset path" in got["i-macro"].message
    assert "macros are not expanded" in got["i-macro"].message
    assert ".bmp, .gif, .ico, .jpeg, .jpg, .png, .svg, .webp" in got["i-macro"].message
    assert "`platform:`" in got["i-platform"].message
    assert len({f.message for f in findings}) == 4


def test_pv_button_on_off_images_are_checked(store: DraftStore) -> None:
    doc = _doc(_node("btn", "pv-button", onImage="on.png", offImage="https://x/off.png"))
    [f] = check_refs(doc, store, "a/top")
    assert (f.node_id, f.path, f.reason, f.kind) == ("btn", "/root/children/0/props/onImage", "missing", "image")


async def test_image_written_through_write_asset_is_present(store: DraftStore) -> None:
    await store.write_asset("a/images/logo.png", PNG)
    assert check_refs(_doc(_image("i", "images/logo.png")), store, "a/top") == []


def test_image_symlinked_outside_the_store_is_missing(store: DraftStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = store.root / "screens" / "a" / "images" / "logo.png"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    [f] = check_refs(_doc(_image("i", "images/logo.png")), store, "a/top")
    assert (f.reason, f.kind) == ("missing", "image")


def test_image_refs_in_a_followed_child_resolve_in_its_own_directory(store: DraftStore) -> None:
    """a/top embeds sub/child; the child's images resolve against a/sub and carry its docId."""
    _put(store, "a/sub/child", _image("c-missing", "gone.png"), _image("c-ok", "ok.png"))
    ok = store.root / "screens" / "a" / "sub" / "ok.png"
    ok.write_bytes(PNG)
    findings = check_refs(_doc(_embed("e", "sub/child.screen.json")), store, "a/top")
    [f] = findings
    d = f.to_dict()
    assert d["nodeId"] == "c-missing"
    assert d["docId"] == "a/sub/child"
    assert (d["code"], d["reason"], d["kind"], d["severity"]) == ("unresolved_ref", "missing", "image", "warning")
    assert "looked for screens/a/sub/gone.png" in d["message"]


def test_image_refs_at_store_root_use_drafts_root_wording(store: DraftStore) -> None:
    [f] = check_refs(_doc(_image("i", "../x.png")), store, None)
    assert f.reason == "above_root"
    assert "resolved against the drafts root" in f.message
