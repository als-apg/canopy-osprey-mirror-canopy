"""The channel walk: which strings in a node's props the renderer subscribes to."""

import ast
from pathlib import Path

import pytest

from canopy_mcp import channels
from canopy_mcp.channels import EMBED_TYPE, UNKNOWN_TYPE, iter_channels

SRC = Path(channels.__file__).parent


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_walk_yields_path_and_string_for_every_channel_key():
    props = {
        "pv": "A",
        "controlPV": "B",
        "limits": {"lowChannel": "C", "high": "not a channel"},
        "curves": [
            {"y_channels": ["D", 7, "E"], "y_channel": "F"},
            {"y_channels": "G", "x_channel": "H"},
        ],
        "text": "not a channel",
        "precision": 3,
    }
    assert list(iter_channels("pv-slider", props)) == [
        (("pv",), "A"),
        (("controlPV",), "B"),
        (("limits", "lowChannel"), "C"),
        (("curves", 0, "y_channels", 0), "D"),
        (("curves", 0, "y_channels", 2), "E"),
        (("curves", 0, "y_channel"), "F"),
        (("curves", 1, "y_channels"), "G"),
        (("curves", 1, "x_channel"), "H"),
    ]


def test_walk_skips_actions_macros_and_rules_at_any_depth():
    props = {
        "actions": [{"type": "write_pv", "pvName": "TOP:ACTION"}],
        "macros": {"pv": "TOP:MACRO"},
        "rules": [{"pvs": [{"channel": "TOP:RULE"}]}],
        "tabs": [
            {
                "pv": "KEPT",
                "actions": [{"pvName": "DEEP:ACTION"}],
                "macros": {"channel": "DEEP:MACRO"},
                "rules": {"pv": "DEEP:RULE"},
            }
        ],
    }
    assert list(iter_channels("pv-tabs", props)) == [(("tabs", 0, "pv"), "KEPT")]


@pytest.mark.parametrize("ntype", [EMBED_TYPE, UNKNOWN_TYPE, "edm-mystery", None, 3])
def test_walk_yields_nothing_for_embed_unknown_and_unmapped(ntype):
    assert list(iter_channels(ntype, {"pv": "A", "channel": "B"})) == []


@pytest.mark.parametrize("props", [None, "pv", ["pv"], 3])
def test_walk_yields_nothing_when_props_is_not_a_dict(props):
    assert list(iter_channels("pv-label", props)) == []


def test_absolute_canvas_mounts_children_and_names_no_mapped_type():
    assert channels.mounts_children("absolute-canvas")
    assert channels.mounts_children("group")
    assert not channels.mounts_children(EMBED_TYPE)
    assert not channels.mounts_children(UNKNOWN_TYPE)
    assert not channels.mounts_children("edm-mystery")
    assert channels.is_unmapped(UNKNOWN_TYPE) and not channels.is_unmapped(EMBED_TYPE)


def test_module_imports_no_reader():
    imported = _imported_modules(SRC / "channels.py")
    assert imported, "the module imports at least the standard library"
    assert not [name for name in imported if name.split(".")[0] == "canopy_mcp"]


def test_semantic_has_no_function_local_import_of_screens():
    tree = ast.parse((SRC / "check" / "semantic.py").read_text(encoding="utf-8"))
    local = [
        node.module
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom)
    ]
    assert local == []
    assert "canopy_mcp.channels" in _imported_modules(SRC / "check" / "semantic.py")
