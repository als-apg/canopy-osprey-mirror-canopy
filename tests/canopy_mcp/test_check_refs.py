"""The ref resolver: chain folding, the two roots, listings and trees, and the findings ``check`` reports."""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

import pytest

from canopy_mcp.store import LIBRARY, DraftStore, InvalidDraftId
from canopy_mcp.catalog import build_catalog
from canopy_mcp.check.refs import (
    StoreTree,
    BufferTree,
    RefFinding,
    resolve,
    fold_ref,
    join_ref,
    iter_refs,
    normalize,
    check_refs,
    resolve_ref,
    screen_path,
    iter_image_refs,
    resolve_image_ref,
)
from canopy_mcp.tools.check import run_check

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_DRAFTS = FIXTURES / "refs" / "drafts"
VECTORS = json.loads((FIXTURES / "expand-vectors.json").read_text())


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


def test_sc6_a_nested_ref_resolves_in_its_own_documents_directory(fixture_store: DraftStore) -> None:
    """a/top embeds sub/child; child's x.screen.json exists at a/sub/x but not at a/x, and it resolves."""
    assert check_refs(_load("a/top"), fixture_store, "a/top") == []


def test_sc6_a_nested_ref_missing_beside_its_document_is_unresolved(store: DraftStore) -> None:
    """The mirror case: x exists beside the top but not beside the child that names it."""
    _put(store, "a/sub/child", _embed("child-embed", "x.screen.json"))
    _put(store, "a/x")
    [f] = check_refs(_doc(_embed("e", "sub/child.screen.json")), store, "a/top")
    assert f.to_dict() == {
        "nodeId": "child-embed",
        "code": "unresolved_ref",
        "path": "/root/children/0/props/file",
        "severity": "warning",
        "message": "ref 'x.screen.json' names no stored draft (resolved against 'a/sub/')",
        "docId": "a/sub/child",
        "reason": "missing",
        "kind": "screen",
    }


def test_no_finding_carries_the_retired_code(fixture_store: DraftStore) -> None:
    for draft_id in fixture_store.list():
        codes = {f.code for f in check_refs(_load(draft_id), fixture_store, draft_id)}
        assert codes <= {"unresolved_ref", "root_relative_ref", "cross_root_ref"}


def test_checking_the_child_itself_resolves_in_its_own_directory(fixture_store: DraftStore) -> None:
    assert check_refs(_load("a/sub/child"), fixture_store, "a/sub/child") == []


# --- one fixture per reason ------------------------------------------------


def test_one_unresolved_ref_per_reason(fixture_store: DraftStore) -> None:
    findings = check_refs(_load("a/reasons"), fixture_store, "a/reasons")
    got = {(f.node_id, f.code, f.path, f.severity, getattr(f, "reason", None), f.doc_id) for f in findings}
    assert got == {
        ("r-absolute", "root_relative_ref", "/root/children/0/props/file", "warning", None, None),
        ("r-absolute", "unresolved_ref", "/root/children/0/props/file", "warning", "absolute", None),
        ("r-above", "unresolved_ref", "/root/children/1/props/file", "warning", "above_root", None),
        ("r-invalid", "unresolved_ref", "/root/children/2/props/file", "warning", "invalid_id", None),
        ("r-missing", "unresolved_ref", "/root/children/3/props/actions/0/file", "warning", "missing", None),
    }
    assert len(findings) == 5
    findings = [f for f in findings if f.code == "unresolved_ref"]
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


def test_a_missing_nested_ref_is_reported_with_its_doc_id(store: DraftStore) -> None:
    """A ref of an embedded document that names no draft is unresolved_ref with docId."""
    _put(store, "a/child", _embed("grand", "gone.screen.json"), _embed("ok", "sib.screen.json"))
    _put(store, "a/sib")
    [f] = check_refs(_doc(_embed("e", "child.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id) == ("grand", "unresolved_ref", "missing", "a/child")
    assert f.path == "/root/children/0/props/file"


def test_nested_open_display_ref_is_checked(store: DraftStore) -> None:
    _put(store, "a/sub/child", _node("btn", "pv-button", actions=[{"type": "open_display", "file": "x.screen.json"}]))
    _put(store, "a/x")
    top = _doc(_embed("e", "sub/child.screen.json"))
    [f] = check_refs(top, store, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id, f.path) == (
        "btn",
        "unresolved_ref",
        "missing",
        "a/sub/child",
        "/root/children/0/props/actions/0/file",
    )
    _put(store, "a/sub/x")
    assert check_refs(top, store, "a/top") == []


def test_open_display_targets_are_not_followed(store: DraftStore) -> None:
    _put(store, "a/other", _embed("deep", "gone.screen.json"))
    doc = _doc(_node("btn", "pv-button", actions=[{"type": "open_display", "file": "other.screen.json"}]))
    assert check_refs(doc, store, "a/top") == []


def test_grandchild_is_followed_through_the_chain_and_reports_doc_id(store: DraftStore) -> None:
    _put(store, "a/one/c1", _embed("to-c2", "../two/c2.screen.json"))
    _put(store, "a/two/c2", _embed("broken", "~/abs.screen.json"), _embed("fine", "../one/c1.screen.json"))
    [f] = check_refs(_doc(_embed("e", "one/c1.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id) == ("broken", "unresolved_ref", "absolute", "a/two/c2")


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


# --- chain folding: the runtime's resolveRef ------------------------------------------


@pytest.mark.parametrize(
    ("top", "chain", "ref", "expected"),
    [
        # screenPath given: every result is root-relative
        ("screens/nest/top.screen.json", [], "sub/mid.screen.json", "screens/nest/sub/mid.screen.json"),
        (
            "screens/nest/top.screen.json",
            ["sub/mid.screen.json"],
            "../parts/inner.v1.screen.json",
            "screens/nest/parts/inner.v1.screen.json",
        ),
        ("screens/ls/top.screen.json", [], "/screens/ls/sub/leaf.screen.json", "screens/ls/sub/leaf.screen.json"),
        ("screens/ls/top.screen.json", [], "/sub/leaf.screen.json", "sub/leaf.screen.json"),
        ("screens/a/top.screen.json", ["/x/mid.screen.json"], "leaf.screen.json", "x/leaf.screen.json"),
        ("screens/top.screen.json", [], "../../up.screen.json", "../up.screen.json"),
        # viewer mode: relative to the top document's directory, which the loader joins
        (None, [], "sub/mid.screen.json", "sub/mid.screen.json"),
        (None, ["sub/mid.screen.json"], "../parts/inner.v1.screen.json", "parts/inner.v1.screen.json"),
        (None, ["B.screen.json"], "../gtb/B.screen.json", "../gtb/B.screen.json"),
        (None, [], "/sub/leaf.screen.json", "sub/leaf.screen.json"),
        (None, ["a/b.screen.json", "./c/d.screen.json"], "e.screen.json", "a/c/e.screen.json"),
    ],
)
def test_fold_ref_is_the_runtimes_resolve_ref(top: str | None, chain: list[str], ref: str, expected: str) -> None:
    assert fold_ref(top, chain, ref) == expected


def _embeds(node: Any) -> list[str]:
    """The embed refs of a document, in document order, not descending into an embed."""
    if not isinstance(node, dict):
        return []
    if node.get("type") == "embedded-display":
        file = node.get("props", {}).get("file")
        return [file] if isinstance(file, str) else []
    return [ref for child in node.get("children", []) for ref in _embeds(child)]


def _reached(case: dict[str, Any]) -> set[str]:
    """The screens a vector case reaches when its embeds are loaded through ``fold_ref`` alone."""
    docs = case["docs"]
    top_file = screen_path(case["top"])
    screen_path_given = top_file if case["screenPath"] is not None else None
    reached = {case["top"]}

    def visit(doc: dict[str, Any], chain: list[str], ancestors: list[str], depth: int) -> None:
        if depth + 1 > 8:
            return
        for file in _embeds(doc.get("root")):
            ref = fold_ref(screen_path_given, chain, file)
            path = ref if screen_path_given else join_ref(top_file, ref)
            key = path.removeprefix("screens/").removesuffix(".screen.json")
            if ref in ancestors or not path.startswith("screens/") or key not in docs:
                continue
            reached.add(key)
            visit(docs[key], [*chain, file], [*ancestors, ref], depth + 1)

    visit(docs[case["top"]], [], [fold_ref(top_file, [], "")] if screen_path_given else [], 0)
    return reached


RESOLVE_REF_CASES = [
    case
    for case in VECTORS["cases"]
    if any(word in case["name"] for word in ("viewer mode", "screenPath given", "leading /", "depth 8"))
    and not case.get("docsRaw")
    and not case.get("invalidDocs")
]


@pytest.mark.parametrize("case", RESOLVE_REF_CASES, ids=[case["name"] for case in RESOLVE_REF_CASES])
def test_chain_folding_reaches_the_screens_the_runtime_vectors_expect(case: dict[str, Any]) -> None:
    assert len(RESOLVE_REF_CASES) >= 8
    assert _reached(case) == {node["screen"] for node in case["expect"]["nodes"]}


def test_resolve_folds_through_the_chain_in_store_path_space() -> None:
    top = "screens/nest/top.screen.json"
    found = resolve(top, ["sub/mid.screen.json"], "../parts/inner.screen.json")
    assert (found.path, found.draft_id, found.reason, found.folded) == (
        "screens/nest/parts/inner.screen.json",
        "nest/parts/inner",
        None,
        "parts/inner.screen.json",
    )
    assert found.ok and not found.root_relative and not found.cross_root
    # the same ref declared by the top document lands one directory up
    assert resolve(top, [], "../parts/inner.screen.json").path == "screens/parts/inner.screen.json"
    image = resolve(top, ["sub/mid.screen.json"], "img/a.png", kind="image")
    assert (image.path, image.relpath, image.draft_id) == ("screens/nest/sub/img/a.png", "nest/sub/img/a.png", None)


def test_resolve_without_a_tree_or_listing_does_not_ask_for_existence() -> None:
    assert resolve("screens/", [], "nowhere.screen.json").ok


# --- root_relative_ref ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("chain", "ref", "folded"),
    [
        ([], "/a/x.screen.json", "/a/x.screen.json"),
        ([], "screens/x.screen.json", "screens/x.screen.json"),
        ([], "./sub/../library/x.screen.json", "library/x.screen.json"),
        (["sub/mid.screen.json"], "../screens/x.screen.json", "screens/x.screen.json"),
        (["/sub/mid.screen.json"], "x.screen.json", "/sub/x.screen.json"),
    ],
)
def test_a_folded_path_that_starts_at_a_root_name_is_root_relative(chain: list[str], ref: str, folded: str) -> None:
    found = resolve("screens/a/top.screen.json", chain, ref)
    assert (found.root_relative, found.folded) == (True, folded)


@pytest.mark.parametrize(
    "ref", ["x.screen.json", "sub/screens/x.screen.json", "screensaver/x.screen.json", "../x.screen.json"]
)
def test_other_refs_are_not_root_relative(ref: str) -> None:
    assert not resolve("screens/a/top.screen.json", [], ref).root_relative


def test_image_refs_are_never_root_relative() -> None:
    found = resolve("screens/a/top.screen.json", [], "screens/logo.png", kind="image")
    assert (found.root_relative, found.path) == (False, "screens/a/screens/logo.png")


def test_root_relative_ref_is_a_warning_in_check_beside_how_the_ref_resolves(store: DraftStore) -> None:
    _put(store, "a/screens/x")
    doc = _doc(_embed("here", "screens/x.screen.json"), _embed("gone", "library/y.screen.json"))
    findings = check_refs(doc, store, "a/top")
    assert [(f.node_id, f.code, f.severity, getattr(f, "reason", None)) for f in findings] == [
        ("here", "root_relative_ref", "warning", None),
        ("gone", "root_relative_ref", "warning", None),
        ("gone", "unresolved_ref", "warning", "missing"),
    ]
    assert findings[0].to_dict() == {
        "nodeId": "here",
        "code": "root_relative_ref",
        "path": "/root/children/0/props/file",
        "severity": "warning",
        "message": findings[0].message,
        "kind": "screen",
    }
    assert "'screens/x.screen.json'" in findings[0].message and "cannot be pushed" in findings[0].message


def test_root_relative_ref_in_an_embedded_document_carries_its_doc_id(store: DraftStore) -> None:
    _put(store, "a/sub/child", _embed("inner", "../screens/x.screen.json"))
    _put(store, "a/screens/x")
    [f] = check_refs(_doc(_embed("e", "sub/child.screen.json")), store, "a/top")
    assert (f.node_id, f.code, f.doc_id) == ("inner", "root_relative_ref", "a/sub/child")
    # checked on its own the same ref folds to '../screens/...' and is an ordinary relative ref
    assert check_refs(json.loads(store.path_for("a/sub/child").read_text()), store, "a/sub/child") == []


# --- cross_root_ref and above_root ----------------------------------------------------


@pytest.mark.parametrize(
    ("served_at", "top", "ref", "expected"),
    [
        ("personal", "screens/top.screen.json", "../library/screens/part.screen.json", "cross"),
        ("personal", "screens/a/top.screen.json", "../../library/screens/b/part.screen.json", "cross"),
        ("personal", "screens/top.screen.json", "../library/part.screen.json", "cross"),
        ("personal", "screens/top.screen.json", "../part.screen.json", "above_root"),
        ("personal", "screens/top.screen.json", "../../part.screen.json", "above_root"),
        ("personal", "screens/library/top.screen.json", "part.screen.json", "screens/library/part.screen.json"),
        ("library", "screens/top.screen.json", "part.screen.json", "screens/part.screen.json"),
        ("library", "screens/a/top.screen.json", "../b/part.screen.json", "screens/b/part.screen.json"),
        ("library", "screens/top.screen.json", "../../screens/part.screen.json", "cross"),
        ("library", "screens/top.screen.json", "../../part.screen.json", "cross"),
        ("library", "screens/top.screen.json", "../part.screen.json", "above_root"),
        ("library", "screens/top.screen.json", "../../../part.screen.json", "above_root"),
    ],
)
def test_where_the_served_url_lands(served_at: str, top: str, ref: str, expected: str) -> None:
    found = resolve(top, [], ref, served_at=served_at)  # type: ignore[arg-type]
    got = "cross" if found.cross_root else found.reason or found.path
    assert got == expected
    assert found.ok == expected.startswith("screens/")


def test_cross_root_ref_is_an_error_that_says_to_copy_the_part(store: DraftStore) -> None:
    doc = _doc(_embed("part", "../library/screens/part.screen.json"), _image("logo", "../library/screens/logo.png"))
    findings = check_refs(doc, store, "top")
    assert [(f.node_id, f.code, f.severity, f.kind, f.reason) for f in findings] == [
        ("part", "cross_root_ref", "error", "screen", None),
        ("logo", "cross_root_ref", "error", "image", None),
    ]
    for f in findings:
        assert "copy the part into your store" in f.message
        assert set(f.to_dict()) == {"nodeId", "code", "path", "severity", "message", "kind"}
    assert "'library/screens/part.screen.json'" in findings[0].message


def test_a_library_screen_that_reaches_into_the_personal_root_is_cross_root(tmp_path: Path) -> None:
    library = DraftStore(tmp_path / "library", read_only=True, category=LIBRARY)
    _put(library, "sub/part")
    doc = _doc(_embed("own", "sub/part.screen.json"), _embed("theirs", "../../screens/mine.screen.json"))
    [f] = check_refs(doc, library, "top")
    assert (f.node_id, f.code, f.severity) == ("theirs", "cross_root_ref", "error")
    # the same document read as a personal one only climbs above its root
    [f] = check_refs(doc, library, "top", served_at="personal")
    assert (f.node_id, f.code, f.severity, f.reason) == ("theirs", "unresolved_ref", "warning", "above_root")


def test_above_root_stays_a_warning(store: DraftStore) -> None:
    [f] = check_refs(_doc(_embed("up", "../../x.screen.json")), store, "a/top")
    assert (f.code, f.severity, f.reason) == ("unresolved_ref", "warning", "above_root")


def test_the_two_argument_classifiers_report_another_root_as_above_root() -> None:
    assert resolve_ref("../library/screens/x.screen.json", "") == (None, "above_root")
    assert resolve_image_ref("../library/screens/x.png", "") == (None, "above_root")


# --- listings and trees ---------------------------------------------------------------


def _bytes(*children: dict[str, Any]) -> bytes:
    return json.dumps(_doc(*children)).encode()


def test_a_case_mismatched_ref_is_unresolved_under_a_listing(store: DraftStore) -> None:
    """File ``Embedded/_X``, ref ``embedded/_X``: a case-insensitive host would find the file; a listing does not."""
    _put(store, "a/Embedded/_X")
    doc = _doc(_embed("exact", "Embedded/_X.screen.json"), _embed("folded", "embedded/_X.screen.json"))
    listing = frozenset({"screens/a/top.screen.json", "screens/a/Embedded/_X.screen.json"})
    [f] = check_refs(doc, store, "a/top", listing=listing)
    assert (f.node_id, f.code, f.reason) == ("folded", "unresolved_ref", "missing")
    assert not resolve("screens/a/top.screen.json", [], "embedded/_X.screen.json", listing=listing).ok
    assert resolve("screens/a/top.screen.json", [], "Embedded/_X.screen.json", listing=listing).ok


def test_a_listing_is_the_only_thing_asked(store: DraftStore) -> None:
    """A stored file that the listing leaves out does not exist; a listed path that is not stored does."""
    _put(store, "a/stored")
    doc = _doc(
        _embed("stored", "stored.screen.json"),
        _node("btn", "pv-button", actions=[{"type": "open_display", "file": "listed.screen.json"}]),
        _image("img", "listed.png"),
        _image("img2", "unlisted.png"),
    )
    listing = frozenset({"screens/a/listed.screen.json", "screens/a/listed.png"})
    findings = check_refs(doc, store, "a/top", listing=listing)
    assert [(f.node_id, f.reason, f.kind) for f in findings] == [
        ("stored", "missing", "screen"),
        ("img2", "missing", "image"),
    ]


def test_a_buffer_tree_resolves_by_exact_path_and_serves_embedded_documents() -> None:
    tree = BufferTree(
        {
            "screens/a/top.screen.json": _bytes(_embed("e", "Sub/child.screen.json")),
            "screens/a/Sub/child.screen.json": _bytes(_embed("deep", "gone.screen.json"), _image("img", "ok.png")),
            "screens/a/Sub/ok.png": PNG,
        }
    )
    assert tree.paths == {"screens/a/top.screen.json", "screens/a/Sub/child.screen.json", "screens/a/Sub/ok.png"}
    [f] = check_refs(json.loads(tree.read("screens/a/top.screen.json")), tree, "a/top")
    assert (f.node_id, f.code, f.reason, f.doc_id) == ("deep", "unresolved_ref", "missing", "a/Sub/child")
    [f] = check_refs(_doc(_embed("e", "sub/child.screen.json")), tree, "a/top")
    assert (f.node_id, f.reason) == ("e", "missing")
    with pytest.raises(FileNotFoundError):
        tree.read("screens/a/sub/child.screen.json")


def test_a_buffer_tree_does_not_see_later_changes_to_the_mapping() -> None:
    buffers = {"screens/x.screen.json": b"{}"}
    tree = BufferTree(buffers)
    buffers["screens/y.screen.json"] = b"{}"
    assert tree.paths == {"screens/x.screen.json"}


async def test_a_store_tree_lists_and_reads_what_the_store_serves(store: DraftStore, tmp_path: Path) -> None:
    _put(store, "a/top", _embed("e", "sub/child.screen.json"))
    _put(store, "a/sub/child")
    await store.write_asset("a/images/logo.png", PNG)
    (store.screens / "a" / "notes.txt").write_text("not served")
    outside = tmp_path / "outside.screen.json"
    outside.write_text("{}")
    (store.screens / "a" / "leak.screen.json").symlink_to(outside)
    tree = StoreTree(store)
    assert tree.paths == {"screens/a/top.screen.json", "screens/a/sub/child.screen.json", "screens/a/images/logo.png"}
    assert tree.read("screens/a/images/logo.png") == PNG
    assert tree.recorded is None
    for refused in ("screens/a/notes.txt", "screens/a/leak.screen.json", "screens/a/../a/top.screen.json", "a/top"):
        with pytest.raises(FileNotFoundError):
            tree.read(refused)


def test_a_store_tree_leaves_out_what_the_store_reserves(tmp_path: Path) -> None:
    personal = DraftStore(tmp_path / "drafts", reserved=(LIBRARY,))
    _put(personal, "library/x")
    _put(personal, "mine/x")
    tree = StoreTree(personal)
    assert tree.paths == {"screens/mine/x.screen.json"}
    assert not tree.has("screens/library/x.screen.json")
    findings = check_refs(_doc(_embed("e", "library/x.screen.json"), _embed("ok", "mine/x.screen.json")), personal)
    assert [(f.node_id, f.code, f.reason) for f in findings] == [
        ("e", "root_relative_ref", None),
        ("e", "unresolved_ref", "missing"),
    ]


def test_a_recording_store_tree_keeps_every_byte_string_it_read(store: DraftStore) -> None:
    """The check run again on a buffer tree of the recorded bytes equals the check on the store."""
    _put(store, "a/top", _embed("e", "sub/child.screen.json"), _embed("solo", "leaf.screen.json"))
    _put(store, "a/sub/child", _embed("deep", "gone.screen.json"), _embed("leaf", "../leaf.screen.json"))
    _put(store, "a/leaf")
    _put(store, "a/unrelated")
    tree = StoreTree(store, record=True)
    top = json.loads(tree.read(screen_path("a/top")))
    first = check_refs(top, tree, "a/top")
    assert tree.recorded is not None
    assert set(tree.recorded) == {
        "screens/a/top.screen.json",
        "screens/a/sub/child.screen.json",
        "screens/a/leaf.screen.json",
    }
    assert tree.recorded["screens/a/leaf.screen.json"] == store.path_for("a/leaf").read_bytes()
    store.path_for("a/sub/child").write_text("{}")  # a later write does not reach the recorded bytes
    again = check_refs(top, BufferTree(tree.recorded), "a/top")
    assert again == first
    assert [(f.node_id, f.reason, f.doc_id) for f in again] == [("deep", "missing", "a/sub/child")]


def _catalog() -> Any:
    ir_map = {name: {"consumes": ["file"], "verbatim": False} for name in ("group", "embedded-display")}
    return build_catalog(ir_map)


async def test_run_check_reads_the_draft_and_its_refs_through_a_tree() -> None:
    """With a tree no store is opened: the stored draft, ref existence and embedded documents come from it."""
    tree = BufferTree(
        {
            "screens/a/top.screen.json": _bytes(_embed("e", "sub/child.screen.json")),
            "screens/a/sub/child.screen.json": _bytes(_embed("deep", "gone.screen.json")),
        }
    )
    stored = await run_check(draft_id="a/top", tree=tree, catalog=_catalog())
    refs = [f for f in stored.semantic if isinstance(f, RefFinding)]
    assert [(f.node_id, f.code, f.reason, f.doc_id) for f in refs] == [
        ("deep", "unresolved_ref", "missing", "a/sub/child")
    ]
    given = await run_check(_doc(_embed("x", "../library/screens/p.screen.json")), "top", tree=tree, catalog=_catalog())
    assert [(f.node_id, f.code) for f in given.errors if isinstance(f, RefFinding)] == [("x", "cross_root_ref")]
    library = await run_check(
        _doc(_embed("x", "../../screens/p.screen.json")), "top", tree=tree, served_at="library", catalog=_catalog()
    )
    assert [(f.node_id, f.code) for f in library.errors if isinstance(f, RefFinding)] == [("x", "cross_root_ref")]


async def test_run_check_on_a_tree_refuses_a_draft_it_does_not_hold() -> None:
    from fastmcp.exceptions import ToolError

    tree = BufferTree({"screens/bad.screen.json": b"{not json"})
    with pytest.raises(ToolError, match="no draft stored under 'gone'"):
        await run_check(draft_id="gone", tree=tree, catalog=_catalog())
    with pytest.raises(ToolError, match="not valid JSON"):
        await run_check(draft_id="bad", tree=tree, catalog=_catalog())
