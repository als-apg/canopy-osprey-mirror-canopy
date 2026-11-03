"""The dock's layout tree, validated and normalised.

    Layout = Tile | Split
    Tile   = {tile, tabs: [key, ...>=1], showing: key in tabs, size?, from?}
    Split  = {split: "row" | "column", items: [Layout, ...>=2], size?}

The dock form (a window's ``state.layout``) is above; ``size`` is the fraction of the
parent along its axis, siblings sum to 1 (+-0.001) and missing sizes share the remainder.
The collection form (a collection's layout, ``open_set``'s layout) has no ``tile`` and no
``from``, ``size`` is a positive weight and a missing ``showing`` means ``tabs[0]``; its
keys are member indexes. No depth cap. This mirrors the viewer's dock layout model
rule for rule: the first broken rule wins in tree order and is named by the same reason
string, so both replay the shared ``validate`` / ``normalize`` cases
(``tests/canopy_mcp/fixtures/dock-cases``).

Shared by :mod:`canopy_mcp.collections` and :mod:`canopy_mcp.workspace`.
"""

from __future__ import annotations

import math
from typing import Any, Literal
from collections.abc import Mapping

__all__ = [
    "LAYOUT_REASONS",
    "SIZE_TOLERANCE",
    "SPLITS",
    "LayoutForm",
    "LayoutRefused",
    "check_layout",
    "normalize_layout",
]

# The named reasons a layout is refused: the viewer's ``LAYOUT_REASONS`` values, the
# vocabulary of the shared ``validate`` cases.
LAYOUT_REASONS = (
    "not_layout",
    "unknown_field",
    "bad_split",
    "bad_tile_id",
    "duplicate_tile",
    "bad_key",
    "empty_tabs",
    "duplicate_key",
    "showing_not_in_tabs",
    "split_too_few_items",
    "bad_size",
    "unknown_from",
    "member_missing",
)
LayoutForm = Literal["dock", "collection"]
SPLITS = ("row", "column")
# Dock form: sibling fractions sum to 1 within this.
SIZE_TOLERANCE = 0.001
_SPLIT_FIELDS = frozenset({"split", "items", "size"})
_DOCK_TILE_FIELDS = frozenset({"tile", "tabs", "showing", "size", "from"})
_COLLECTION_TILE_FIELDS = frozenset({"tabs", "showing", "size"})


class LayoutRefused(ValueError):
    """A layout that breaks a layout rule: ``reason`` is one of :data:`LAYOUT_REASONS`, ``where`` its path."""

    def __init__(self, reason: str, where: str) -> None:
        super().__init__(f"{where}: {reason}")
        self.reason = reason
        self.where = where


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    """A JSON number that is finite (an int of any length is)."""
    return _is_number(value) and (isinstance(value, int) or math.isfinite(value))


def _is_index(value: Any) -> bool:
    """A JSON number that is a non-negative integer (``2.0`` counts, as in JavaScript)."""
    if not _is_number(value) or value < 0:
        return False
    return isinstance(value, int) or (math.isfinite(value) and value.is_integer())


def _same(a: Any, b: Any) -> bool:
    """JavaScript's ``SameValueZero`` for JSON keys: numbers by value, strings by text, never across."""
    if _is_number(a) and _is_number(b):
        return a == b
    return isinstance(a, str) and isinstance(b, str) and a == b


class _Walk:
    def __init__(self, form: str, members: int | None):
        self.form = form
        self.members = members
        self.keys: list[Any] = []
        self.tiles: set[str] = set()
        self.froms: list[tuple[str, str]] = []

    def placed(self, key: Any) -> bool:
        return any(_same(key, seen) for seen in self.keys)


def _check_key(key: Any, walk: _Walk, where: str) -> None:
    if walk.form == "dock":
        if not isinstance(key, str) or key == "":
            raise LayoutRefused("bad_key", where)
        return
    if walk.members is not None:
        if not _is_index(key) or key >= walk.members:
            raise LayoutRefused("bad_key", where)
        return
    if not (_is_index(key) or (isinstance(key, str) and key != "")):
        raise LayoutRefused("bad_key", where)


def _check_size(node: Mapping[str, Any], form: str, where: str) -> None:
    if "size" not in node:
        return
    size = node["size"]
    ok = _finite(size) and size > 0 and (form == "collection" or size <= 1 + SIZE_TOLERANCE)
    if not ok:
        raise LayoutRefused("bad_size", where)


def _check_fractions(items: list[Mapping[str, Any]], where: str) -> None:
    """Dock form: given fractions sum to 1, or leave a remainder for the missing ones."""
    given = 0.0
    missing = 0
    for item in items:
        if _is_number(item.get("size")):
            given += item["size"]
        else:
            missing += 1
    ok = abs(given - 1) <= SIZE_TOLERANCE if missing == 0 else given < 1 - SIZE_TOLERANCE
    if not ok:
        raise LayoutRefused("bad_size", where)


def _check_fields(node: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(field) for field in node if field not in allowed)
    if unknown:
        raise LayoutRefused("unknown_field", f"{where}.{unknown[0]}")


def _check_tile(node: Mapping[str, Any], walk: _Walk, where: str) -> None:
    _check_fields(node, _DOCK_TILE_FIELDS if walk.form == "dock" else _COLLECTION_TILE_FIELDS, where)
    tile_id = ""
    if walk.form == "dock":
        tile_id = node.get("tile")
        if not isinstance(tile_id, str) or tile_id == "":
            raise LayoutRefused("bad_tile_id", f"{where}.tile")
        if tile_id in walk.tiles:
            raise LayoutRefused("duplicate_tile", f"{where}.tile")
        walk.tiles.add(tile_id)
    tabs = node.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        raise LayoutRefused("empty_tabs", f"{where}.tabs")
    for index, key in enumerate(tabs):
        at = f"{where}.tabs[{index}]"
        _check_key(key, walk, at)
        if walk.placed(key):
            raise LayoutRefused("duplicate_key", at)
        walk.keys.append(key)
    if "showing" in node or walk.form == "dock":
        if not any(_same(node.get("showing"), key) for key in tabs):
            raise LayoutRefused("showing_not_in_tabs", f"{where}.showing")
    _check_size(node, walk.form, f"{where}.size")
    if "from" in node:
        origin = node["from"]
        if not isinstance(origin, str) or origin == tile_id:
            raise LayoutRefused("unknown_from", f"{where}.from")
        walk.froms.append((origin, f"{where}.from"))


def _check_node(value: Any, walk: _Walk, where: str) -> None:
    if not isinstance(value, Mapping):
        raise LayoutRefused("not_layout", where)
    if "split" not in value and "items" not in value:
        if "tabs" not in value and "tile" not in value:
            raise LayoutRefused("not_layout", where)
        _check_tile(value, walk, where)
        return
    _check_fields(value, _SPLIT_FIELDS, where)
    if value.get("split") not in SPLITS:
        raise LayoutRefused("bad_split", f"{where}.split")
    items = value.get("items")
    if not isinstance(items, list) or len(items) < 2:
        raise LayoutRefused("split_too_few_items", f"{where}.items")
    _check_size(value, walk.form, f"{where}.size")
    for index, item in enumerate(items):
        _check_node(item, walk, f"{where}.items[{index}]")
    if walk.form == "dock":
        _check_fractions(items, f"{where}.items")


def check_layout(layout: Any, form: LayoutForm = "dock", members: int | None = None) -> list[Any]:
    """The layout's keys in tree order; :class:`LayoutRefused` naming the first broken rule.

    ``form`` is ``dock`` (default; tile ids, string keys, fractions) or ``collection`` (no
    ids, weights, ``showing`` optional); with ``members`` (collection form only) every key
    must be a member index below it and every member placed once. A tree too deep for
    Python's recursion raises :class:`RecursionError`.
    """
    walk = _Walk(form, members if form == "collection" else None)
    _check_node(layout, walk, "layout")
    for origin, where in walk.froms:
        if origin not in walk.tiles:
            raise LayoutRefused("unknown_from", where)
    if walk.members is not None:
        for index in range(walk.members):
            if not walk.placed(index):
                raise LayoutRefused("member_missing", "layout")
    return walk.keys


def _fractions(items: list[Mapping[str, Any]], form: str) -> list[float]:
    """Dock form: missing sizes share the remainder; collection form: a missing weight is 1. Then sum to 1."""
    given = [item["size"] if _finite(item.get("size")) and item["size"] > 0 else None for item in items]
    missing = sum(1 for size in given if size is None)
    fill = 1.0
    if form == "dock" and missing:
        remainder = 1 - sum(size for size in given if size is not None)
        fill = remainder / missing if remainder > 1e-9 else 1 / len(items)
    raw = [fill if size is None else size for size in given]
    total = sum(raw)
    return [size / total for size in raw]


def _with_size(node: Mapping[str, Any], size: Any) -> dict[str, Any]:
    out = dict(node)
    if size is None:
        out.pop("size", None)
    else:
        out["size"] = size
    return out


def _normalize_node(node: Mapping[str, Any], form: str, tiles: set[str]) -> dict[str, Any]:
    if "split" not in node:
        out = {**node, "tabs": list(node["tabs"])}
        if "showing" not in out:
            out["showing"] = out["tabs"][0]
        if "from" in out and (
            not isinstance(out["from"], str) or out["from"] not in tiles or out["from"] == out.get("tile")
        ):
            del out["from"]
        return out
    fractions = _fractions(node["items"], form)
    items: list[dict[str, Any]] = []
    for share, item in zip(fractions, node["items"], strict=True):
        child = _normalize_node(item, form, tiles)
        if "split" in child and child["split"] == node["split"]:
            items.extend(_with_size(grandchild, share * (grandchild.get("size") or 0)) for grandchild in child["items"])
        else:
            items.append(_with_size(child, share))
    if len(items) == 1:
        return _with_size(items[0], node.get("size"))
    return _with_size({"split": node["split"], "items": items}, node.get("size"))


def _tile_ids(node: Mapping[str, Any], out: set[str]) -> None:
    if "split" in node:
        for item in node["items"]:
            _tile_ids(item, out)
    elif isinstance(node.get("tile"), str):
        out.add(node["tile"])


def normalize_layout(layout: Mapping[str, Any], form: LayoutForm = "dock") -> dict[str, Any]:
    """A valid ``layout`` in normalised form, as a fresh tree (the viewer's ``normalize`` /
    ``normalizeCollection``).

    Same-direction splits are flattened, one-item splits collapsed, every size filled in
    as a fraction with siblings summing to 1 (collection weights normalised, a missing
    weight counting 1), every ``showing`` filled in (default ``tabs[0]``) and a ``from``
    whose tile is gone cleared. The root carries no size.
    """
    tiles: set[str] = set()
    _tile_ids(layout, tiles)
    return _with_size(_normalize_node(layout, form, tiles), None)
