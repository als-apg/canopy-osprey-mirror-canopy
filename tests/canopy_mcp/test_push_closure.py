"""The push closure: the files a screen needs once its directory is copied, and what makes it unpushable.

``library_screens.push_closure(tree, id, source_dir)`` returns the store paths of the
screen, of every screen it embeds or opens and of every stored image they show,
and raises ``PushRefused`` for a ref or a rule that would not mean the same
thing at the destination. Trees are buffer trees unless the case is about the
filesystem, so every answer is the same on a case-insensitive host.
"""

import json

import pytest

from canopy_mcp import library_screens
from canopy_mcp.store import DraftStore
from canopy_mcp.check.refs import StoreTree, BufferTree

PNG = b"\x89PNG\r\n\x1a\n"


def _screen(*children) -> dict:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "s",
        "metadata": {"title": "s", "source": {"type": "hand-authored"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": {"id": "root", "type": "absolute-canvas", "props": {}, "children": list(children)},
    }


def _w(nid: str, ntype: str, children=None, rules=None, **props) -> dict:
    node = {"id": nid, "type": ntype, "props": props}
    if children is not None:
        node["children"] = children
    if rules is not None:
        node["rules"] = rules
    return node


def _embed(nid: str, file: str, **props) -> dict:
    return _w(nid, "embedded-display", file=file, **props)


def _opens(nid: str, file: str) -> dict:
    return _w(nid, "regular-button", actions=[{"type": "open_display", "file": file}])


def _rule(target: str) -> dict:
    return {"targetProperty": target, "pvs": [{"name": "SEL"}], "expressions": []}


def _tree(files: dict) -> BufferTree:
    """A tree of ``{path below screens/: document, or bytes for an image}``."""
    return BufferTree(
        {
            f"screens/{path}": data if isinstance(data, bytes) else json.dumps(data).encode()
            for path, data in files.items()
        }
    )


def _refused(tree, draft_id: str, source_dir: str, **kwargs) -> list[tuple]:
    with pytest.raises(library_screens.PushRefused) as caught:
        library_screens.push_closure(tree, draft_id, source_dir, **kwargs)
    return [(r["code"], r["doc"], r["nodeId"], r["ref"], r["reason"]) for r in caught.value.refusals]


LEAF = _screen(_w("l", "pv-label", pv="LEAF"))


# --- the members ------------------------------------------------------------------------


def test_members_are_the_screen_its_embeds_its_open_display_targets_and_their_images():
    tree = _tree(
        {
            "a/top.screen.json": _screen(
                _embed("e", "parts/mid.screen.json"),
                _opens("go", "detail.screen.json"),
                _w("img", "image-view", src="img/logo.png"),
                _w("btn", "pv-button", pv="X", onImage="img/on.png", offImage="https://example.org/off.png"),
                _w("abs", "image-view", src="/static/served.png"),
                _embed("blank", ""),
            ),
            # an embedded screen's refs resolve beside it
            "a/parts/mid.screen.json": _screen(_embed("e2", "leaf.screen.json"), _w("i", "image-view", src="m.png")),
            "a/parts/leaf.screen.json": LEAF,
            "a/parts/m.png": PNG,
            # an opened screen is a top of its own, and its refs are followed too
            "a/detail.screen.json": _screen(_embed("d", "parts/leaf.screen.json"), _opens("back", "top.screen.json")),
            "a/img/logo.png": PNG,
            "a/img/on.png": PNG,
            "a/unrelated.screen.json": LEAF,
        }
    )
    assert library_screens.push_closure(tree, "a/top", "a") == [
        "screens/a/detail.screen.json",
        "screens/a/img/logo.png",
        "screens/a/img/on.png",
        "screens/a/parts/leaf.screen.json",
        "screens/a/parts/m.png",
        "screens/a/parts/mid.screen.json",
        "screens/a/top.screen.json",
    ]


def test_a_top_level_screen_closes_over_the_store_top_level():
    tree = _tree({"top.screen.json": _screen(_embed("e", "sub/leaf.screen.json")), "sub/leaf.screen.json": LEAF})
    assert library_screens.push_closure(tree, "top", "") == ["screens/sub/leaf.screen.json", "screens/top.screen.json"]


def test_a_ref_may_climb_as_long_as_it_stays_inside_source_dir():
    tree = _tree(
        {
            "a/sub/top.screen.json": _screen(_embed("e", "../shared/part.screen.json")),
            "a/shared/part.screen.json": _screen(_embed("e2", "./deep/../leaf.screen.json")),
            "a/shared/leaf.screen.json": LEAF,
        }
    )
    assert library_screens.push_closure(tree, "a/sub/top", "a") == [
        "screens/a/shared/leaf.screen.json",
        "screens/a/shared/part.screen.json",
        "screens/a/sub/top.screen.json",
    ]


def test_an_embed_cycle_ends():
    tree = _tree(
        {
            "a/top.screen.json": _screen(_embed("e", "other.screen.json"), _embed("self", "top.screen.json")),
            "a/other.screen.json": _screen(_embed("e", "top.screen.json")),
        }
    )
    assert library_screens.push_closure(tree, "a/top", "a") == [
        "screens/a/other.screen.json",
        "screens/a/top.screen.json",
    ]


def test_every_member_is_read_through_the_tree(tmp_path):
    """A recording store tree ends up holding exactly the members, and the closure over those bytes is the same."""
    files = {
        "a/top.screen.json": _screen(_embed("e", "leaf.screen.json"), _w("img", "image-view", src="logo.png")),
        "a/leaf.screen.json": LEAF,
        "a/logo.png": PNG,
        "a/unrelated.screen.json": LEAF,
    }
    for path, data in files.items():
        file = tmp_path / "screens" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    tree = StoreTree(DraftStore(tmp_path), record=True)
    members = library_screens.push_closure(tree, "a/top", "a")
    assert members == ["screens/a/leaf.screen.json", "screens/a/logo.png", "screens/a/top.screen.json"]
    assert sorted(tree.recorded) == members
    assert library_screens.push_closure(BufferTree(tree.recorded), "a/top", "a") == members


# --- hidden nodes -----------------------------------------------------------------------


def test_a_base_hidden_embeds_file_is_a_member():
    tree = _tree(
        {
            "a/top.screen.json": _screen(
                _embed("E1", "one.screen.json", visible=False),
                _w("HC", "group", visible=False, children=[_embed("E2", "two.screen.json")]),
            ),
            "a/one.screen.json": LEAF,
            "a/two.screen.json": LEAF,
        }
    )
    assert library_screens.push_closure(tree, "a/top", "a") == [
        "screens/a/one.screen.json",
        "screens/a/top.screen.json",
        "screens/a/two.screen.json",
    ]


def test_a_base_hidden_embed_with_an_unresolved_ref_is_refused():
    tree = _tree({"a/top.screen.json": _screen(_embed("E1", "gone.screen.json", visible=False))})
    assert _refused(tree, "a/top", "a") == [
        ("unresolved_ref", "screens/a/top.screen.json", "E1", "gone.screen.json", "missing")
    ]


# --- the refusals -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node", "reason"),
    [
        (_embed("n", "gone.screen.json"), "missing"),
        (_opens("n", "gone.screen.json"), "missing"),
        (_w("n", "image-view", src="gone.png"), "missing"),
        (_embed("n", "dotted.v1.screen.json"), "invalid_id"),
        (_embed("n", "~/home.screen.json"), "absolute"),
        (_w("n", "image-view", src="file:logo.png"), "invalid_ref"),
        (_embed("n", "broken.screen.json"), "unreadable"),
    ],
    ids=["embed", "open_display", "image", "invalid_id", "absolute", "image-scheme", "not-a-document"],
)
def test_an_unresolved_ref_is_refused(node, reason):
    tree = _tree(
        {"a/top.screen.json": _screen(node), "a/dotted.v1.screen.json": LEAF, "a/broken.screen.json": b"{ not json"}
    )
    [(code, doc, nid, _ref, got)] = _refused(tree, "a/top", "a")
    assert (code, doc, nid, got) == ("unresolved_ref", "screens/a/top.screen.json", "n", reason)


def test_a_missing_top_is_refused():
    assert _refused(_tree({}), "a/top", "a") == [
        ("unresolved_ref", "screens/a/top.screen.json", None, "a/top", "unreadable")
    ]


@pytest.mark.parametrize(
    "node",
    [
        _embed("n", "${PART}.screen.json"),
        _embed("n", "$(PART).screen.json"),
        _opens("n", "${PART}.screen.json"),
        _opens("n", "$(PART).screen.json"),
        _w("n", "image-view", src="${ICON}.png"),
        _w("n", "pv-button", pv="X", onImage="$(ICON).png"),
    ],
    ids=["embed-brace", "embed-paren", "open-brace", "open-paren", "image-brace", "image-paren"],
)
def test_a_macro_of_either_form_in_a_ref_is_refused(node):
    # the file the macro-free spelling would name exists: it is the macro that is refused
    tree = _tree({"a/top.screen.json": _screen(node), "a/${PART}.screen.json": LEAF, "a/$(PART).screen.json": LEAF})
    [(code, doc, nid, _ref, _reason)] = _refused(tree, "a/top", "a")
    assert (code, doc, nid) == ("macro_ref", "screens/a/top.screen.json", "n")


@pytest.mark.parametrize("target", ["file", "src", "onImage", "offImage", "actions", "macros", "actions[0].file"])
def test_a_rule_that_retargets_a_ref_bearing_prop_is_refused(target):
    # on the targetProperty alone: the node need not be of a type that reads the prop
    tree = _tree({"a/top.screen.json": _screen(_w("n", "pv-label", pv="X", rules=[_rule("color"), _rule(target)]))})
    assert _refused(tree, "a/top", "a") == [("rule_retargets_ref", "screens/a/top.screen.json", "n", target, None)]


@pytest.mark.parametrize(
    "target", ["pv", "pvName", "channel", "x_channel", "y_channels", "readbackChannel", "onPV", "traces[1].pv", "y.pv"]
)
def test_a_rule_that_retargets_a_channel_is_refused(target):
    tree = _tree({"a/top.screen.json": _screen(_w("n", "pv-slider", pv="X", rules=[_rule(target)]))})
    assert _refused(tree, "a/top", "a") == [("rule_retargets_channel", "screens/a/top.screen.json", "n", target, None)]


def test_a_retargeting_rule_is_refused_in_an_embedded_screen_and_under_a_hidden_node():
    tree = _tree(
        {
            "a/top.screen.json": _screen(_embed("e", "part.screen.json", visible=False)),
            "a/part.screen.json": _screen(
                _w("g", "group", visible=False, children=[_w("n", "pv-button", pv="X", rules=[_rule("pv")])])
            ),
        }
    )
    assert _refused(tree, "a/top", "a") == [("rule_retargets_channel", "screens/a/part.screen.json", "n", "pv", None)]


def test_other_rules_are_not_refused():
    tree = _tree(
        {
            "a/top.screen.json": _screen(
                _w("n", "pv-label", pv="X", rules=[_rule("color"), _rule("visible"), _rule("style.color"), {}, 3])
            )
        }
    )
    assert library_screens.push_closure(tree, "a/top", "a") == ["screens/a/top.screen.json"]


@pytest.mark.parametrize(
    "node",
    [
        _embed("n", "../library/screens/common/part.screen.json"),
        _opens("n", "../library/screens/common/part.screen.json"),
        _w("n", "image-view", src="../library/screens/common/logo.png"),
    ],
    ids=["embed", "open_display", "image"],
)
def test_a_cross_root_ref_is_refused(node):
    tree = _tree({"top.screen.json": _screen(node)})
    [(code, doc, nid, _ref, _reason)] = _refused(tree, "top", "")
    assert (code, doc, nid) == ("cross_root_ref", "screens/top.screen.json", "n")


@pytest.mark.parametrize(
    "ref",
    ["/a/part.screen.json", "screens/part.screen.json", "library/part.screen.json"],
    ids=["slash", "screens", "library"],
)
def test_a_root_relative_ref_is_refused_as_written(ref):
    # each spelling names a stored file, so it is refused for being root-relative and for nothing else
    tree = _tree(
        {
            "a/top.screen.json": _screen(_embed("n", ref)),
            "a/part.screen.json": LEAF,
            "a/screens/part.screen.json": LEAF,
            "a/library/part.screen.json": LEAF,
        }
    )
    assert _refused(tree, "a/top", "a") == [("root_relative_ref", "screens/a/top.screen.json", "n", ref, None)]


def test_a_root_relative_ref_is_refused_when_only_the_folded_path_is():
    """``sub/a`` embeds ``../screens/b``: neither spelling is root-relative, the path the viewer folds them to is."""
    tree = _tree(
        {
            "top.screen.json": _screen(_embed("e", "sub/a.screen.json")),
            "sub/a.screen.json": _screen(_embed("n", "../screens/b.screen.json")),
            "screens/b.screen.json": LEAF,
        }
    )
    assert _refused(tree, "top", "") == [
        ("root_relative_ref", "screens/sub/a.screen.json", "n", "../screens/b.screen.json", None)
    ]


@pytest.mark.parametrize(
    "node",
    [
        _embed("n", "../a/part.screen.json"),
        _opens("n", "../a/part.screen.json"),
        _w("n", "image-view", src="../a/logo.png"),
        _embed("n", "sub/../../a/part.screen.json"),
    ],
    ids=["embed", "open_display", "image", "after-a-descent"],
)
def test_a_ref_that_leaves_source_dir_and_returns_is_refused(node):
    # the folded path is inside `a/` and the file is stored: only the spelling leaves
    tree = _tree({"a/top.screen.json": _screen(node), "a/part.screen.json": LEAF, "a/logo.png": PNG})
    [(code, doc, nid, _ref, _reason)] = _refused(tree, "a/top", "a")
    assert (code, doc, nid) == ("ref_leaves_source", "screens/a/top.screen.json", "n")


def test_a_ref_that_leaves_source_dir_for_good_is_refused():
    tree = _tree({"a/top.screen.json": _screen(_embed("n", "../b/part.screen.json")), "b/part.screen.json": LEAF})
    assert _refused(tree, "a/top", "a") == [
        ("ref_leaves_source", "screens/a/top.screen.json", "n", "../b/part.screen.json", None)
    ]
    # the same ref is inside a wider source_dir
    assert library_screens.push_closure(tree, "a/top", "") == [
        "screens/a/top.screen.json",
        "screens/b/part.screen.json",
    ]


def test_leaving_is_judged_from_the_declaring_document():
    tree = _tree(
        {
            "a/top.screen.json": _screen(_embed("e", "deep/part.screen.json")),
            "a/deep/part.screen.json": _screen(
                _embed("up", "../leaf.screen.json"), _embed("out", "../../a/leaf.screen.json")
            ),
            "a/leaf.screen.json": LEAF,
        }
    )
    assert _refused(tree, "a/top", "a") == [
        ("ref_leaves_source", "screens/a/deep/part.screen.json", "out", "../../a/leaf.screen.json", None)
    ]


@pytest.mark.parametrize(
    ("draft_id", "source_dir"),
    [("a/top", "b"), ("ab/top", "a"), ("a/top", "a/top"), ("a/top", "../a"), ("a/top", "/a"), ("a/top.v1", "a")],
)
def test_a_screen_outside_source_dir_is_refused(draft_id, source_dir):
    tree = _tree({"a/top.screen.json": LEAF, "ab/top.screen.json": LEAF, "a/top.v1.screen.json": LEAF})
    [(code, _doc, _nid, ref, _reason)] = _refused(tree, draft_id, source_dir)
    assert (code, ref) == ("outside_source", draft_id)


def test_every_refusal_is_reported():
    tree = _tree(
        {
            "a/top.screen.json": _screen(
                _embed("m", "${X}.screen.json"),
                _embed("g", "gone.screen.json"),
                _embed("ok", "part.screen.json"),
            ),
            "a/part.screen.json": _screen(_w("r", "pv-slider", pv="X", rules=[_rule("pv")])),
        }
    )
    with pytest.raises(library_screens.PushRefused) as caught:
        library_screens.push_closure(tree, "a/top", "a")
    assert caught.value.codes == ["macro_ref", "unresolved_ref", "rule_retargets_channel"]
    assert caught.value.code == "macro_ref"
    assert isinstance(caught.value, ValueError)
    assert "screens/a/part.screen.json" in str(caught.value)


# --- listings ---------------------------------------------------------------------------


def test_a_case_mismatched_ref_is_unresolved_under_a_listing(tmp_path):
    """File ``Embedded/_X``, ref ``embedded/_X``: a case-insensitive host would open the file; a listing does not."""
    top = _screen(_embed("folded", "embedded/_X.screen.json"))
    for path, doc in (("a/top.screen.json", top), ("a/Embedded/_X.screen.json", LEAF)):
        file = tmp_path / "screens" / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(doc), encoding="utf-8")
    tree = StoreTree(DraftStore(tmp_path))
    listing = frozenset({"screens/a/top.screen.json", "screens/a/Embedded/_X.screen.json"})
    assert _refused(tree, "a/top", "a", listing=listing) == [
        ("unresolved_ref", "screens/a/top.screen.json", "folded", "embedded/_X.screen.json", "missing")
    ]


def test_a_stored_file_the_listing_leaves_out_is_unresolved():
    tree = _tree({"a/top.screen.json": _screen(_embed("e", "part.screen.json")), "a/part.screen.json": LEAF})
    assert library_screens.push_closure(tree, "a/top", "a", listing=tree.paths) == [
        "screens/a/part.screen.json",
        "screens/a/top.screen.json",
    ]
    assert _refused(tree, "a/top", "a", listing=frozenset({"screens/a/top.screen.json"})) == [
        ("unresolved_ref", "screens/a/top.screen.json", "e", "part.screen.json", "missing")
    ]


def test_a_top_the_listing_leaves_out_is_unresolved():
    tree = _tree({"a/top.screen.json": LEAF})
    assert _refused(tree, "a/top", "a", listing=frozenset()) == [
        ("unresolved_ref", "screens/a/top.screen.json", None, "a/top", "unreadable")
    ]
