"""The screen walk: what the viewer mounts for a stored screen, and the PVs it binds.

A port of the runtime's ``expandScreen`` (``canopy-screen-runtime``
``packages/runtime/src/render/collectPVs.ts``), the one executable definition
of the walk. The runtime pins it as test vectors (``expand-vectors.json``, homed
in the sdk as ``packages/screen-ir/test-vectors/expand.json``); canopy's copy is
``tests/canopy_mcp/fixtures/expand-vectors.json`` and every case runs against
this module.

- :func:`expand` lists every node the renderer mounts, per screen instance (the
  top screen, then each embedded screen in document order), with its
  macro-substituted base props. It does not descend into the children of an
  ``embedded-display`` (its embed expands through ``file``), an
  ``unknown-widget`` or an unmapped type.
- :func:`expanded_pvs` is the runtime collector's PV list, sorted and unique.
- :func:`visits` is the walk itself, raw node beside expanded node, for the
  callers that judge what a node does (the write-binding listing).

**Hidden nodes.** A node whose substituted base props carry the boolean
``visible: false`` is neither listed nor descended into, and its channels are
not collected: the rule the runtime, the renderer and the vectors share. An
embed takes its index from that walk alone: a hidden embed that names a file
keeps the index it would have had and is not expanded, an embed under a hidden
node takes none. ``include_hidden=True`` (the safety listings) also lists the
hidden nodes and what is under them, with ``hidden: true``, and expands their
embeds. The indices do not move: a hidden embed's instance uses the index it
reserved (``/0:E1``), an embed under a hidden node has none and is keyed
``<parent>/h:<node id>``.

**Loading embeds.** Refs fold through the chain of embedding documents with
``check.refs``'s ``fold_ref``, and an embed is loaded by file path under
``screens/`` (never through the draft-id rule, so a dotted or spaced name loads
as the viewer loads it). The files come from a tree (``check.refs.Tree``); a
``DraftStore`` is read through its ``StoreTree``. With a ``listing``, an embed
loads only when its path is in the listing as that exact string, and is read by
that string: the answer is the same on a case-insensitive host. An embed that
names no file, climbs above the root, leaves ``screens/``, holds text that is
not a JSON document or declares an unsupported ``schemaVersion`` is skipped and
keeps its index, as the renderer shows an error box there.

Stdlib only, besides ``canopy_mcp.store``, ``canopy_mcp.check`` and
``canopy_mcp.channels`` (the node set and the channel-key rule).
"""

from __future__ import annotations

import re
import json
from typing import Any, TypedDict
from dataclasses import dataclass
from collections.abc import Iterator, Collection

from canopy_mcp.store import DraftStore
from canopy_mcp.channels import (
    EMBED_TYPE,
    LOC_PREFIX,
    FORMULA_PREFIX,
    iter_channels,
    mounts_children,
)
from canopy_mcp.check.refs import (
    Tree,
    StoreTree,
    exists,
    fold_ref,
    join_ref,
    screen_key,
    screen_path,
    normalize_path,
)
from canopy_mcp.check.schema import SUPPORTED_VERSIONS

__all__ = [
    "ExpandedNode",
    "Visit",
    "expand",
    "expanded_pvs",
    "has_macro",
    "is_unresolved",
    "macro_names",
    "props_of",
    "raw_channels",
    "substitute",
    "visits",
]

MAX_DEPTH = 8

# `${NAME}` or `$(NAME)`; ASCII word characters, as JavaScript's `\w`.
_MACRO_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")
# Upper bound on default-substitution passes, as the runtime's resolveScreenMacros.
_MAX_DEFAULT_PASSES = 5

Macros = dict[str, str]


class ExpandedNode(TypedDict):
    """One node the renderer mounts, in one screen instance.

    The vectors' node shape (``screen``, ``instance``, ``nodeId``, ``type``,
    ``props``, ``ruleChannels``, ``machineBound``) plus ``macros`` (the
    instance's effective macros), ``unresolvedMacro`` (a substituted prop string
    still holds ``${`` or ``$(``, or a macro it uses took an empty default) and
    ``hidden`` (the node, an ancestor or an embedding node carries a base
    ``visible: false``; only ``include_hidden=True`` lists such a node, so it is
    ``False`` on every node of the default walk).
    """

    screen: str
    instance: str
    nodeId: str
    type: str
    props: dict[str, Any]
    ruleChannels: list[str]
    machineBound: bool
    macros: Macros
    unresolvedMacro: bool
    hidden: bool


# --- macros ----------------------------------------------------------------------


def _macro_name(match: re.Match[str]) -> str:
    return match.group(1) if match.group(1) is not None else match.group(2)


def substitute(template: str, macros: Macros) -> str:
    """``template`` with every macro ``macros`` names replaced; the others stay as written."""

    def repl(match: re.Match[str]) -> str:
        value = macros.get(_macro_name(match))
        return value if isinstance(value, str) else match.group(0)

    return _MACRO_RE.sub(repl, template)


def has_macro(template: str) -> bool:
    """Whether ``template`` holds a ``${NAME}`` or ``$(NAME)`` reference."""
    return _MACRO_RE.search(template) is not None


def macro_names(template: str) -> set[str]:
    """The names of every ``${NAME}`` and ``$(NAME)`` reference in ``template``."""
    return {_macro_name(m) for m in _MACRO_RE.finditer(template)}


def _substitute_deep(value: Any, macros: Macros) -> Any:
    if isinstance(value, str):
        return substitute(value, macros)
    if isinstance(value, list):
        return [_substitute_deep(item, macros) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_deep(item, macros) for key, item in value.items()}
    return value


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _resolve_screen_macros(doc: dict[str, Any], provided: Macros) -> tuple[Macros, set[str]]:
    """The screen's effective macros, and the defaulted names that came out empty."""
    merged: Macros = {}
    defaulted: list[str] = []
    declared = doc.get("macros")
    for decl in declared if isinstance(declared, list) else []:
        if not isinstance(decl, dict) or not isinstance(decl.get("default"), str):
            continue
        name = decl.get("name")
        if not isinstance(name, str):
            continue
        merged[name] = decl["default"]
        if name not in provided:
            defaulted.append(name)
    merged.update(provided)
    for _ in range(_MAX_DEFAULT_PASSES):
        changed = False
        for name in defaulted:
            current = merged.get(name, "")
            nxt = substitute(current, merged)
            if nxt != current:
                merged[name] = nxt
                changed = True
        if not changed:
            break
    return merged, {name for name in defaulted if merged.get(name) == ""}


def _child_macros(parent: Macros, node_macros: dict[str, Any] | None) -> Macros:
    resolved = dict(parent)
    for name, value in (node_macros or {}).items():
        if isinstance(value, str):
            resolved[name] = substitute(value, parent)
    return resolved


def is_unresolved(written: str, resolved: str, empty_defaults: set[str]) -> bool:
    """Whether ``resolved`` (``written`` substituted) still holds a macro or used one that took an empty default."""
    return "${" in resolved or "$(" in resolved or bool(macro_names(written) & empty_defaults)


# --- node set and channels ----------------------------------------------------------


def raw_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    """The PV channel strings a node's props name, as they stand (none for an embed or unmapped type)."""
    return [name for _, name in iter_channels(ntype, props)]


def _node_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    """The PV channels a node's (substituted) props name: trimmed, blanks dropped, nothing else dropped."""
    return [name.strip() for name in raw_channels(ntype, props) if name.strip()]


def _find_formula(formulas: Any, name: Any) -> dict[str, Any] | None:
    for formula in formulas if isinstance(formulas, list) else []:
        if isinstance(formula, dict) and formula.get("name") == name:
            return formula
    return None


def _bindings(formula: dict[str, Any] | None) -> list[str]:
    bindings = formula.get("bindings") if formula else None
    return [b for b in bindings.values() if isinstance(b, str)] if isinstance(bindings, dict) else []


def _rule_list(rules: Any) -> list[dict[str, Any]]:
    return [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []


def _rule_pv_names(rule: dict[str, Any]) -> list[str]:
    pvs = rule.get("pvs")
    names = [pv.get("name") for pv in pvs if isinstance(pv, dict)] if isinstance(pvs, list) else []
    return [name for name in names if isinstance(name, str)]


def _rule_channels(rules: Any, formulas: Any, macros: Macros) -> list[str]:
    out: list[str] = []
    for rule in _rule_list(rules):
        if "formula" in rule:
            out += [substitute(b, macros) for b in _bindings(_find_formula(formulas, rule["formula"]))]
        else:
            out += [substitute(name, macros) for name in _rule_pv_names(rule)]
    return [name.strip() for name in out if name.strip()]


class _Group:
    """One instance's PVs as the runtime's GroupBuilder adds them (dedupe is the caller's set)."""

    def __init__(self, pvs: set[str], formulas: Any, macros: Macros):
        self.pvs = pvs
        self.formulas = formulas
        self.macros = macros
        self.used: set[Any] = set()

    def add(self, raw: str) -> None:
        name = raw.strip()
        if not name or name.startswith(LOC_PREFIX) or has_macro(name):
            return
        if name.startswith(FORMULA_PREFIX):
            self.use_formula(name[len(FORMULA_PREFIX) :])
            return
        self.pvs.add(name)

    def use_formula(self, name: str) -> None:
        if name in self.used:
            return
        self.used.add(name)
        for binding in _bindings(_find_formula(self.formulas, name)):
            self.add(substitute(binding, self.macros))

    def rules(self, rules: Any) -> None:
        for rule in _rule_list(rules):
            if "formula" in rule:
                self.use_formula(rule["formula"])
            else:
                for name in _rule_pv_names(rule):
                    self.add(substitute(name, self.macros))


# --- loading embeds -------------------------------------------------------------------


def _as_tree(store: DraftStore | Tree) -> Tree:
    return StoreTree(store) if isinstance(store, DraftStore) else store


def _load(tree: Tree, path: str, listing: Collection[str] | None) -> dict[str, Any] | None:
    """The screen document at store path ``path``, or ``None`` when the walk cannot load one there."""
    if screen_key(path) is None or not exists(tree, path, listing):
        return None
    try:
        doc = json.loads(tree.read(path))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schemaVersion") not in SUPPORTED_VERSIONS:
        return None
    return doc


# --- the walk -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Instance:
    doc: dict[str, Any]
    macros: Macros
    empty_defaults: set[str]
    depth: int
    key: str
    screen_ref: str
    chain: list[str]
    ancestors: list[str]
    hidden: bool


@dataclass(frozen=True)
class Visit:
    """One mounted node: the node as written, as expanded, and its instance's empty-defaulted macro names."""

    raw: dict[str, Any]
    node: ExpandedNode
    empty_defaults: set[str]


def props_of(node: dict[str, Any]) -> dict[str, Any]:
    """A node's base props as written, or ``{}`` when it carries none."""
    props = node.get("props")
    return props if isinstance(props, dict) else {}


_SHOWN, _HIDDEN, _UNDER_HIDDEN = 0, 1, 2


def _walk_mounted(root: Any, macros: Macros):
    """``(raw node, substituted props, state)`` for every node of one document, in document order.

    ``state`` is ``_SHOWN`` for a node the renderer walks, ``_HIDDEN`` for a node
    whose own base props hide it and ``_UNDER_HIDDEN`` for everything below one.
    """

    def walk(node: Any, chain: list[Any], state: int):
        if not isinstance(node, dict) or node.get("id") in chain:
            return
        props = _substitute_deep(props_of(node), macros)
        if state != _SHOWN:
            state = _UNDER_HIDDEN
        elif props.get("visible") is False:
            state = _HIDDEN
        yield node, props, state
        if not mounts_children(node.get("type")):
            return
        children = node.get("children")
        for child in children if isinstance(children, list) else []:
            yield from walk(child, [*chain, node.get("id")], state)

    yield from walk(root, [], _SHOWN)


def _embed_slots(doc: dict[str, Any], macros: Macros, include_hidden: bool):
    """``(slot, node, file, node macros, hidden)`` per embed that names a file.

    ``slot`` is the runtime's embedOrder index: counted over the shown embeds
    and the hidden ones that reserve theirs, whatever ``include_hidden`` says.
    An embed under a hidden node has none (``None``). Hidden embeds are yielded
    only with ``include_hidden``.
    """
    index = 0
    for node, props, state in _walk_mounted(doc.get("root"), macros):
        if node.get("type") != EMBED_TYPE:
            continue
        file = props.get("file")
        if not isinstance(file, str) or file == "":
            continue
        slot = None
        if state != _UNDER_HIDDEN:
            slot = index
            index += 1
        if state != _SHOWN and not include_hidden:
            continue
        node_macros = props.get("macros")
        yield (
            slot,
            node,
            substitute(file, macros),
            node_macros if isinstance(node_macros, dict) else None,
            state != _SHOWN,
        )


def _embed_instance(
    parent: _Instance,
    slot: int | None,
    node: dict[str, Any],
    file: str,
    node_macros: dict[str, Any] | None,
    hidden: bool,
    ref: str,
    child: dict[str, Any],
) -> _Instance:
    """The instance an embed slot of ``parent`` mounts for the loaded ``child`` document."""
    passed = _child_macros(parent.macros, node_macros)
    # A name stays empty-defaulted unless the embed sets it; a value the
    # embed authors non-empty that comes out empty took an empty default.
    authored = props_of(node).get("macros")
    authored = authored if isinstance(authored, dict) else {}
    inherited = {name for name in parent.empty_defaults if name not in (node_macros or {})}
    inherited |= {
        name for name, value in authored.items() if isinstance(value, str) and value != "" and passed.get(name) == ""
    }
    child_macros, child_empty = _resolve_screen_macros(child, passed)
    return _Instance(
        doc=child,
        macros=child_macros,
        empty_defaults=inherited | child_empty,
        depth=parent.depth + 1,
        key=f"{parent.key}/{'h' if slot is None else slot}:{node.get('id')}",
        screen_ref=ref,
        chain=[*parent.chain, file],
        ancestors=[*parent.ancestors, ref],
        hidden=parent.hidden or hidden,
    )


def _walk(
    doc: dict[str, Any],
    store: DraftStore | Tree,
    draft_id: str,
    macros: Macros | None,
    top_on_path: bool,
    pvs: set[str],
    *,
    include_hidden: bool = False,
    listing: Collection[str] | None = None,
):
    tree = _as_tree(store)
    top_file = screen_path(draft_id)
    top_path = top_file if top_on_path else None
    provided = dict(macros or {})
    top_macros, top_empty = _resolve_screen_macros(doc, provided)

    def path_of(ref: str) -> str:
        """The store path of a folded ref: as given with the top on the path, else joined to the top's directory."""
        return ref if top_on_path else join_ref(top_file, ref)

    def visit(instance: _Instance):
        current = instance.doc
        formulas = current.get("formulas")
        group = _Group(pvs, formulas, instance.macros)
        screen = draft_id if instance.key == "" else screen_key(path_of(instance.screen_ref))
        for raw, props, state in _walk_mounted(current.get("root"), instance.macros):
            if state != _SHOWN and not include_hidden:
                continue
            ntype = raw.get("type")
            channels = _node_channels(ntype, props)
            rule_channels = _rule_channels(raw.get("rules"), formulas, instance.macros)
            unresolved = any(
                is_unresolved(written, substitute(written, instance.macros), instance.empty_defaults)
                for written in _strings(props_of(raw))
            )
            node: ExpandedNode = {
                "screen": screen or "",
                "instance": instance.key,
                "nodeId": raw.get("id"),
                "type": ntype,
                "props": props,
                "ruleChannels": rule_channels,
                "machineBound": any(not name.startswith(LOC_PREFIX) for name in [*channels, *rule_channels]),
                "macros": dict(instance.macros),
                "unresolvedMacro": unresolved,
                "hidden": instance.hidden or state != _SHOWN,
            }
            yield Visit(raw, node, instance.empty_defaults)
            for name in channels:
                group.add(name)
            group.rules(raw.get("rules"))

        if instance.depth + 1 > MAX_DEPTH:
            return
        children: list[_Instance] = []
        for slot, node, file, node_macros, node_hidden in _embed_slots(current, instance.macros, include_hidden):
            ref = fold_ref(top_path, instance.chain, file)
            if ref in instance.ancestors:
                continue
            child = _load(tree, path_of(ref), listing)
            if child is None:
                continue
            children.append(_embed_instance(instance, slot, node, file, node_macros, node_hidden, ref, child))
        for child_instance in children:
            yield from visit(child_instance)

    yield from visit(
        _Instance(
            doc=doc,
            macros=top_macros,
            empty_defaults=top_empty,
            depth=0,
            key="",
            screen_ref=top_path or "",
            chain=[],
            ancestors=[normalize_path(top_path)] if top_path else [],
            hidden=False,
        )
    )


def expand(
    doc: dict[str, Any],
    store: DraftStore | Tree,
    draft_id: str,
    *,
    macros: Macros | None = None,
    top_on_path: bool = False,
    include_hidden: bool = False,
    listing: Collection[str] | None = None,
) -> list[ExpandedNode]:
    """Every node the renderer mounts for ``doc`` (stored as ``draft_id``), per screen instance.

    ``top_on_path=False`` is the viewer's mode: the top document is not on the
    cycle path and embed refs resolve against its directory. ``True`` puts the
    top on the path as ``screens/<draft_id>.screen.json`` (the vectors'
    ``screenPath``-given mode). ``macros`` are the caller's values, taken
    verbatim over the screen's defaults. ``include_hidden`` also lists the
    statically hidden nodes and what is under them (see the module text).
    ``store`` is a :class:`DraftStore` or a tree; with a ``listing``, an embed
    loads only when its exact path string is a member of it.
    """
    walk = _walk(doc, store, draft_id, macros, top_on_path, set(), include_hidden=include_hidden, listing=listing)
    return [visit.node for visit in walk]


def expanded_pvs(
    doc: dict[str, Any],
    store: DraftStore | Tree,
    draft_id: str,
    *,
    macros: Macros | None = None,
    top_on_path: bool = False,
    listing: Collection[str] | None = None,
) -> list[str]:
    """The PVs the runtime's collector lists for ``doc``, sorted (as JavaScript sorts) and unique.

    Prop channels of mounted, mapped nodes and rule PVs, trimmed; ``loc://``
    names, names with an unresolved macro and blanks dropped; a ``formula://`` name
    stands for the inputs of the formula it names (and of formulas those use),
    read from the declaring screen's formulas. Actions are not read, and a
    statically hidden node opens no subscription, so its channels are left out.
    """
    pvs: set[str] = set()
    for _ in _walk(doc, store, draft_id, macros, top_on_path, pvs, listing=listing):
        pass
    return sorted(pvs, key=lambda name: name.encode("utf-16-be"))


def visits(
    doc: dict[str, Any],
    store: DraftStore | Tree,
    draft_id: str,
    *,
    macros: Macros | None = None,
    include_hidden: bool = False,
    listing: Collection[str] | None = None,
) -> Iterator[Visit]:
    """The walk :func:`expand` lists, in the viewer's mode, with each node's raw form beside it.

    A caller that judges what a node does (a write, a ref) reads the raw node
    for what was written and the expanded node for what it resolves to.
    """
    return _walk(doc, store, draft_id, macros, False, set(), include_hidden=include_hidden, listing=listing)
