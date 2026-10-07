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
- :func:`list_screen_write_bindings` lists every PV one stored screen can
  write; :func:`list_write_bindings` does so for every screen of a store.
- :func:`push_closure` lists the files a screen needs once it is copied
  elsewhere, and refuses a screen whose refs would not survive the copy.

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

Not an MCP tool: the tool count is unchanged. Stdlib only, besides
``canopy_mcp.store`` and ``canopy_mcp.check``.
"""

from __future__ import annotations

import re
import json
from typing import Any, TypedDict
from dataclasses import dataclass
from collections.abc import Collection

from canopy_mcp.store import ID_PATTERN, SCREEN_SUFFIX, DraftStore
from canopy_mcp.check.refs import (
    IMAGE_PROPS,
    Tree,
    Resolved,
    StoreTree,
    exists,
    resolve,
    fold_ref,
    join_ref,
    iter_refs,
    screen_id,
    screen_path,
    normalize_path,
    iter_image_refs,
)
from canopy_mcp.check.schema import SUPPORTED_VERSIONS
from canopy_mcp.check.semantic import INERT_IN_OSPREY_TYPES, iter_nodes

__all__ = [
    "MAPPED_TYPES",
    "RULE_REF_PROPS",
    "ExpandedNode",
    "WriteBinding",
    "PushRefused",
    "Refusal",
    "expand",
    "expanded_pvs",
    "list_screen_write_bindings",
    "list_write_bindings",
    "push_closure",
]

#: The IR types the renderer maps onto a widget (its ``TYPE_MAP``), sorted;
#: ``absolute-canvas`` and ``embedded-display`` are drawn by the renderer itself
#: and not listed. A test asserts this equals the vectors' ``mappedTypes``.
MAPPED_TYPES: tuple[str, ...] = (
    "arc",
    "ellipse",
    "frame",
    "group",
    "group-box",
    "image-view",
    "line",
    "polygon",
    "pv-array-plot",
    "pv-button",
    "pv-byte-led",
    "pv-checkbox",
    "pv-enum-combobox",
    "pv-gauge",
    "pv-label",
    "pv-led",
    "pv-meter",
    "pv-progress-bar",
    "pv-radio-group",
    "pv-slider",
    "pv-spinbox",
    "pv-tabs",
    "pv-tank",
    "pv-text-input",
    "pv-thermometer",
    "pv-toggle",
    "qwidget-container",
    "rectangle",
    "regular-button",
    "related-display-button",
    "scroll-area",
    "shell-command-button",
    "tabs",
    "text-label",
    "waveform-plot",
    "xy-plot",
)
_MAPPED = frozenset(MAPPED_TYPES)

EMBED_TYPE = "embedded-display"
UNKNOWN_TYPE = "unknown-widget"
_RENDERER_NATIVE_TYPES = frozenset({"absolute-canvas", EMBED_TYPE})
MAX_DEPTH = 8
ROOT_DIR = "screens/"
LOC_PREFIX = "loc://"
FOX_PREFIX = "fox://"
WRITE_PV_ACTION = "write_pv"

# `${NAME}` or `$(NAME)`; ASCII word characters, as JavaScript's `\w`.
_MACRO_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)")
# Prop keys that name a PV channel, at any depth (JS `^(...)$`, so fullmatch).
_CHANNEL_KEY = re.compile(r"pv|pvName|channel|[A-Za-z0-9]+_channels?|[a-z][A-Za-z0-9]*(?:Channel|PV)")
# Props never read for channels.
_SKIPPED_PROPS = frozenset({"actions", "macros", "rules"})
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


class WriteBinding(TypedDict):
    """One PV a screen can write. ``top`` is the top-level screen the row was reached from."""

    top: str
    screen: str
    instance: str
    nodeId: str
    kind: str
    pvAsWritten: str
    pvResolved: str
    local: bool
    unresolvedMacro: bool


# --- macros ----------------------------------------------------------------------


def _macro_name(match: re.Match[str]) -> str:
    return match.group(1) if match.group(1) is not None else match.group(2)


def _substitute(template: str, macros: Macros) -> str:
    def repl(match: re.Match[str]) -> str:
        value = macros.get(_macro_name(match))
        return value if isinstance(value, str) else match.group(0)

    return _MACRO_RE.sub(repl, template)


def _has_macro(template: str) -> bool:
    return _MACRO_RE.search(template) is not None


def _macro_names(template: str) -> set[str]:
    return {_macro_name(m) for m in _MACRO_RE.finditer(template)}


def _substitute_deep(value: Any, macros: Macros) -> Any:
    if isinstance(value, str):
        return _substitute(value, macros)
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
            nxt = _substitute(current, merged)
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
            resolved[name] = _substitute(value, parent)
    return resolved


def _is_unresolved(written: str, resolved: str, empty_defaults: set[str]) -> bool:
    return "${" in resolved or "$(" in resolved or bool(_macro_names(written) & empty_defaults)


# --- node set and channels ----------------------------------------------------------


def _is_unmapped(ntype: Any) -> bool:
    if not isinstance(ntype, str):
        return True
    return ntype == UNKNOWN_TYPE or (ntype not in _MAPPED and ntype not in _RENDERER_NATIVE_TYPES)


def _mounts_children(ntype: Any) -> bool:
    return ntype != EMBED_TYPE and not _is_unmapped(ntype)


def _channels_of(value: Any, key: str | None, out: list[str]) -> None:
    if isinstance(value, str):
        if key is not None and _CHANNEL_KEY.fullmatch(key):
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            _channels_of(item, key, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in _SKIPPED_PROPS:
                _channels_of(v, k, out)


def _raw_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    """The PV channel strings a node's props name, as they stand (none for an embed or unmapped type)."""
    if ntype == EMBED_TYPE or _is_unmapped(ntype):
        return []
    names: list[str] = []
    for key, value in props.items():
        if key not in _SKIPPED_PROPS:
            _channels_of(value, key, names)
    return names


def _node_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    """The PV channels a node's (substituted) props name: trimmed, blanks dropped, nothing else dropped."""
    return [name.strip() for name in _raw_channels(ntype, props) if name.strip()]


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
            out += [_substitute(b, macros) for b in _bindings(_find_formula(formulas, rule["formula"]))]
        else:
            out += [_substitute(name, macros) for name in _rule_pv_names(rule)]
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
        if not name or name.startswith(LOC_PREFIX) or _has_macro(name):
            return
        if name.startswith(FOX_PREFIX):
            self.use_formula(name[len(FOX_PREFIX) :])
            return
        self.pvs.add(name)

    def use_formula(self, name: str) -> None:
        if name in self.used:
            return
        self.used.add(name)
        for binding in _bindings(_find_formula(self.formulas, name)):
            self.add(_substitute(binding, self.macros))

    def rules(self, rules: Any) -> None:
        for rule in _rule_list(rules):
            if "formula" in rule:
                self.use_formula(rule["formula"])
            else:
                for name in _rule_pv_names(rule):
                    self.add(_substitute(name, self.macros))


# --- loading embeds -------------------------------------------------------------------


def _as_tree(store: DraftStore | Tree) -> Tree:
    return StoreTree(store) if isinstance(store, DraftStore) else store


def _screen_key(path: str) -> str | None:
    """The name of a screen file below ``screens/``, without the id rule (a dotted name is a key)."""
    if not path.startswith(ROOT_DIR) or not path.endswith(SCREEN_SUFFIX):
        return None
    return path[len(ROOT_DIR) : len(path) - len(SCREEN_SUFFIX)]


def _load(tree: Tree, path: str, listing: Collection[str] | None) -> dict[str, Any] | None:
    """The screen document at store path ``path``, or ``None`` when the walk cannot load one there."""
    if _screen_key(path) is None or not exists(tree, path, listing):
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
class _Visit:
    """One mounted node with what ``expand`` and ``list_write_bindings`` both need."""

    raw: dict[str, Any]
    node: ExpandedNode
    empty_defaults: set[str]


def _props_of(node: dict[str, Any]) -> dict[str, Any]:
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
        props = _substitute_deep(_props_of(node), macros)
        if state != _SHOWN:
            state = _UNDER_HIDDEN
        elif props.get("visible") is False:
            state = _HIDDEN
        yield node, props, state
        if not _mounts_children(node.get("type")):
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
            _substitute(file, macros),
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
    authored = _props_of(node).get("macros")
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
        screen = draft_id if instance.key == "" else _screen_key(path_of(instance.screen_ref))
        for raw, props, state in _walk_mounted(current.get("root"), instance.macros):
            if state != _SHOWN and not include_hidden:
                continue
            ntype = raw.get("type")
            channels = _node_channels(ntype, props)
            rule_channels = _rule_channels(raw.get("rules"), formulas, instance.macros)
            unresolved = any(
                _is_unresolved(written, _substitute(written, instance.macros), instance.empty_defaults)
                for written in _strings(_props_of(raw))
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
            yield _Visit(raw, node, instance.empty_defaults)
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
    names, names with an unresolved macro and blanks dropped; a ``fox://`` name
    stands for the inputs of the formula it names (and of formulas those use),
    read from the declaring screen's formulas. Actions are not read, and a
    statically hidden node opens no subscription, so its channels are left out.
    """
    pvs: set[str] = set()
    for _ in _walk(doc, store, draft_id, macros, top_on_path, pvs, listing=listing):
        pass
    return sorted(pvs, key=lambda name: name.encode("utf-16-be"))


# --- write bindings -------------------------------------------------------------------


def _read_top(store: DraftStore | Tree, draft_id: str, listing: Collection[str] | None) -> dict[str, Any] | None:
    """The stored top-level screen ``draft_id``, or ``None`` when there is none to read."""
    path = screen_path(draft_id)
    if listing is not None and path not in listing:
        return None
    try:
        doc = store.read(draft_id)[0] if isinstance(store, DraftStore) else json.loads(store.read(path))
    except (LookupError, OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def list_screen_write_bindings(
    store: DraftStore | Tree,
    draft_id: str,
    *,
    listing: Collection[str] | None = None,
) -> list[WriteBinding]:
    """Every PV the stored top-level screen ``draft_id`` can write, one row per binding.

    The screen is expanded in the viewer's mode with no caller macros and with
    ``include_hidden=True``: a rule can show a statically hidden widget, so its
    bindings count. A row is a ``write_pv`` action's ``pvName`` (``kind``
    ``write_pv``) or a channel of a write-capable widget (``kind`` its type, one
    of ``INERT_IN_OSPREY_TYPES``). ``top`` is ``draft_id``; ``screen`` and
    ``instance`` say where under it the node sits. ``pvResolved`` is the
    substituted, trimmed name; a blank one is no binding. ``unresolvedMacro`` is
    true when it still holds ``${`` or ``$(`` or a macro it uses took an empty
    default. Rows that are equal in every key are listed once. A screen that
    cannot be read has no rows.
    """
    doc = _read_top(store, draft_id, listing)
    if doc is None:
        return []
    rows: list[WriteBinding] = []
    seen: set[str] = set()
    for visit in _walk(doc, store, draft_id, None, False, set(), include_hidden=True, listing=listing):
        node, raw = visit.node, visit.raw
        raw_props = _props_of(raw)
        written: list[tuple[str, str]] = []
        if node["type"] in INERT_IN_OSPREY_TYPES:
            written += [(node["type"], name) for name in _raw_channels(node["type"], raw_props)]
        actions = raw_props.get("actions")
        for action in actions if isinstance(actions, list) else []:
            if (
                isinstance(action, dict)
                and action.get("type") == WRITE_PV_ACTION
                and isinstance(action.get("pvName"), str)
            ):
                written.append((WRITE_PV_ACTION, action["pvName"]))
        for kind, pv in written:
            resolved = _substitute(pv, node["macros"]).strip()
            if not resolved:
                continue
            row: WriteBinding = {
                "top": draft_id,
                "screen": node["screen"],
                "instance": node["instance"],
                "nodeId": node["nodeId"],
                "kind": kind,
                "pvAsWritten": pv,
                "pvResolved": resolved,
                "local": resolved.startswith(LOC_PREFIX),
                "unresolvedMacro": _is_unresolved(pv, resolved, visit.empty_defaults),
            }
            identity = json.dumps(row, sort_keys=True, default=repr)
            if identity not in seen:
                seen.add(identity)
                rows.append(row)
    return rows


def list_write_bindings(
    store: DraftStore | Tree,
    *,
    listing: Collection[str] | None = None,
) -> list[WriteBinding]:
    """Every PV the stored screens can write: :func:`list_screen_write_bindings` of each top-level screen.

    The top-level screens are the manifest entries of a :class:`DraftStore`, or
    the paths of a tree that name a draft id (of the ``listing`` when one is
    given), in id order.
    """
    if isinstance(store, DraftStore):
        ids = [entry["id"] for entry in store.manifest()["screens"]]
    else:
        ids = sorted(filter(None, map(screen_id, store.paths if listing is None else listing)))
    rows: list[WriteBinding] = []
    for draft_id in ids:
        rows += list_screen_write_bindings(store, draft_id, listing=listing)
    return rows


# --- the push closure -----------------------------------------------------------------

#: Props a screen ref, an image ref, an action or an embed's macros are read
#: from: a rule that retargets one changes what the screen loads, does or binds
#: after it was reviewed.
RULE_REF_PROPS = frozenset({"file", "actions", "macros", *(name for names in IMAGE_PROPS.values() for name in names)})
# A targetProperty is judged by each name in it, so `actions[0].file` and `style.pv` count.
_TARGET_SEPARATORS = re.compile(r"[.\[\]]+")


def _rule_retargets(target: str) -> str | None:
    """The refusal code for a rule's ``targetProperty``, judged on that string alone."""
    names = [name.strip() for name in _TARGET_SEPARATORS.split(target)]
    if any(name in RULE_REF_PROPS for name in names):
        return "rule_retargets_ref"
    if any(_CHANNEL_KEY.fullmatch(name) for name in names):
        return "rule_retargets_channel"
    return None


class Refusal(TypedDict):
    """Why :func:`push_closure` refuses a screen: one ref or one rule.

    ``code`` is ``unresolved_ref`` (with ``reason``, a resolver reason or
    ``unreadable``), ``macro_ref``, ``rule_retargets_ref``,
    ``rule_retargets_channel``, ``cross_root_ref``, ``root_relative_ref``,
    ``ref_leaves_source`` or ``outside_source``. ``doc`` is the store path of
    the document that declares it, ``ref`` the ref as written (the rule's
    ``targetProperty`` for the two rule codes).
    """

    code: str
    doc: str
    nodeId: Any
    ref: str
    reason: str | None
    message: str


class PushRefused(ValueError):
    """A screen cannot be pushed as it stands; ``refusals`` lists every reason found."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("; ".join(refusal["message"] for refusal in refusals))

    @property
    def code(self) -> str:
        """The code of the first refusal."""
        return self.refusals[0]["code"]

    @property
    def codes(self) -> list[str]:
        return [refusal["code"] for refusal in self.refusals]


def _leaves(doc_path: str, ref: str, source: str) -> bool:
    """Whether ``ref``, read segment by segment from the directory of ``doc_path``, ever steps above ``source``.

    Folding hides this: ``../other/../here/x`` folds to a path inside the
    directory it left. The spelling is what gets copied, and at the destination
    the directory above is a different one.
    """
    depth = doc_path[len(source) :].count("/")
    for segment in ref.split("/"):
        if segment in ("", "."):
            continue
        depth += -1 if segment == ".." else 1
        if depth < 0:
            return True
    return False


def _ref_refusal(found: Resolved, doc_path: str, source: str) -> tuple[str, str | None, str] | None:
    """``(code, reason, message)`` when the push refuses the ref ``found`` of ``doc_path``, else ``None``."""
    ref = found.ref
    if _has_macro(ref):
        return "macro_ref", None, f"ref {ref!r} holds a macro; the file it names cannot be told before it is opened"
    if found.external:
        return None
    if found.cross_root:
        return "cross_root_ref", None, f"ref {ref!r} lands in the other root (at {found.served!r})"
    if found.root_relative:
        return (
            "root_relative_ref",
            None,
            f"ref {ref!r} is root-relative (it folds to {found.folded!r}): it is matched against the whole "
            "store before the declaring screen's directory",
        )
    if _leaves(doc_path, ref, source):
        return "ref_leaves_source", None, f"ref {ref!r} steps above the pushed directory {source!r}"
    if not found.ok:
        return "unresolved_ref", found.reason, f"ref {ref!r} is unresolved ({found.reason})"
    return None


def push_closure(
    tree: Tree,
    id: str,  # noqa: A002 - the name the contract gives it
    source_dir: str,
    *,
    listing: Collection[str] | None = None,
) -> list[str]:
    """The store paths of every file screen ``id`` needs when the directory ``source_dir`` is copied elsewhere.

    The members are the screen itself, every screen it embeds or opens with an
    ``open_display`` action, the same for each of those, and every stored image
    they show; sorted, each read through ``tree.read`` (so a recording tree
    holds exactly the members). Every node counts, hidden or not, mounted or
    not. ``source_dir`` is a directory in draft-id space (``""`` for the store's
    top level) that holds ``id``; a member's path below
    ``screens/<source_dir>/`` is where it goes at the destination. With a
    ``listing``, a ref exists only as an exact member of it.

    Raises :class:`PushRefused`, listing every refusal, when a ref is
    unresolved, holds a macro of either form, lands in the other root, is
    root-relative, or steps above ``source_dir`` at any point (even if it comes
    back), and when a rule's ``targetProperty`` names one of ``RULE_REF_PROPS``
    or a PV channel key (whole, or as one name of a dotted or indexed target).
    """
    source = f"{ROOT_DIR}{source_dir}/" if source_dir else ROOT_DIR
    top = screen_path(id)
    refusals: list[Refusal] = []

    def refuse(code: str, doc: str, nid: Any, ref: str, reason: str | None, message: str) -> None:
        refusals.append(
            {"code": code, "doc": doc, "nodeId": nid, "ref": ref, "reason": reason, "message": f"{doc}: {message}"}
        )

    if (source_dir and not ID_PATTERN.fullmatch(source_dir)) or screen_id(top) is None or not top.startswith(source):
        refuse("outside_source", top, None, id, None, f"screen {id!r} is not a screen inside {source_dir!r}")
        raise PushRefused(refusals)

    def read(path: str, doc: str, nid: Any, ref: str) -> bytes | None:
        try:
            if listing is not None and path not in listing:
                raise FileNotFoundError(path)
            return tree.read(path)
        except OSError:
            refuse("unresolved_ref", doc, nid, ref, "unreadable", f"ref {ref!r} names a file that cannot be read")
            return None

    def found_of(top_path: str, chain: tuple[str, ...], ref: str, kind: Any, doc: str, nid: Any) -> Resolved | None:
        """The resolved ref when the push takes it and it names a stored file."""
        found = resolve(top_path, chain, ref, kind=kind, tree=tree, listing=listing)
        refusal = _ref_refusal(found, doc, source)
        if refusal is not None:
            refuse(refusal[0], doc, nid, ref, refusal[1], refusal[2])
            return None
        return None if found.external else found

    members: set[str] = set()
    seen: set[tuple[str, str]] = set()
    # (top document, document, chain from the top, then who named it)
    pending: list[tuple[str, str, tuple[str, ...], str, Any, str]] = [(top, top, (), top, None, id)]
    while pending:
        top_path, doc_path, chain, declared_in, declared_by, declared_as = pending.pop(0)
        if (top_path, doc_path) in seen:
            continue
        seen.add((top_path, doc_path))
        data = read(doc_path, declared_in, declared_by, declared_as)
        if data is None:
            continue
        members.add(doc_path)
        try:
            doc = json.loads(data)
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            message = f"ref {declared_as!r} names a file that is not a screen document"
            refuse("unresolved_ref", declared_in, declared_by, declared_as, "unreadable", message)
            continue

        for _path, node in iter_nodes(doc):
            for rule in _rule_list(node.get("rules")):
                target = rule.get("targetProperty")
                if not isinstance(target, str):
                    continue
                code = _rule_retargets(target)
                if code == "rule_retargets_ref":
                    message = f"a rule retargets {target!r}, which names what the screen loads, does or binds"
                    refuse(code, doc_path, node.get("id"), target, None, message)
                elif code is not None:
                    message = f"a rule retargets the PV channel {target!r}"
                    refuse(code, doc_path, node.get("id"), target, None, message)

        for nid, _path, ref, embedded in iter_refs(doc):
            if ref == "":
                continue  # names no file: nothing is loaded
            found = found_of(top_path, chain, ref, "screen", doc_path, nid)
            if found is None or found.path is None:
                continue
            if embedded:
                pending.append((top_path, found.path, (*chain, ref), doc_path, nid, ref))
            else:
                pending.append((found.path, found.path, (), doc_path, nid, ref))

        for nid, _path, ref in iter_image_refs(doc):
            found = found_of(top_path, chain, ref, "image", doc_path, nid)
            if found is not None and found.path is not None and found.path not in members:
                if read(found.path, doc_path, nid, ref) is not None:
                    members.add(found.path)

    if refusals:
        raise PushRefused(refusals)
    return sorted(members)
