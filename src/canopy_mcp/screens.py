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
- :func:`list_write_bindings` lists every PV a stored screen can write.

Embeds are resolved here, by file path under ``<root>/screens/`` (never through
``DraftStore``'s id rule, so a dotted or spaced name loads as the viewer loads
it, and never through ``check.refs``, which resolves against the top document).
An embed that names no file, climbs above the root, leaves ``screens/``, holds
text that is not a JSON document or declares an unsupported ``schemaVersion``
is skipped and keeps its index, as the renderer shows an error box there.

Not an MCP tool: the tool count is unchanged. Stdlib only, besides
``canopy_mcp.store`` and the two check constants.
"""

from __future__ import annotations

import re
import json
from typing import Any, TypedDict
from dataclasses import dataclass

from canopy_mcp.store import SCREEN_SUFFIX, DraftStore, contained_file
from canopy_mcp.check.schema import SUPPORTED_VERSIONS
from canopy_mcp.check.semantic import INERT_IN_OSPREY_TYPES

__all__ = [
    "MAPPED_TYPES",
    "ExpandedNode",
    "WriteBinding",
    "expand",
    "expanded_pvs",
    "list_write_bindings",
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
    ``hidden`` (the node, an ancestor or the embedding node carries a base
    ``visible: false``: the renderer lists it in its walk but never mounts it).
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
    screen: str
    instance: str
    nodeId: str
    kind: str
    pvAsWritten: str
    pvResolved: str
    local: bool
    unresolvedMacro: bool


# --- macros ----------------------------------------------------------------------


def _substitute(template: str, macros: Macros) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1) if match.group(1) is not None else match.group(2)
        value = macros.get(name)
        return value if isinstance(value, str) else match.group(0)

    return _MACRO_RE.sub(repl, template)


def _has_macro(template: str) -> bool:
    return _MACRO_RE.search(template) is not None


def _macro_names(template: str) -> set[str]:
    return {m.group(1) if m.group(1) is not None else m.group(2) for m in _MACRO_RE.finditer(template)}


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


def _node_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    """The PV channels a node's (substituted) props name: trimmed, blanks dropped, nothing else dropped."""
    if ntype == EMBED_TYPE or _is_unmapped(ntype):
        return []
    names: list[str] = []
    for key, value in props.items():
        if key not in _SKIPPED_PROPS:
            _channels_of(value, key, names)
    return [name.strip() for name in names if name.strip()]


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


# --- refs and the resolver ----------------------------------------------------------


def _normalise(path: str) -> str:
    out: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." and out and out[-1] != "..":
            out.pop()
        else:
            out.append(seg)
    return "/".join(out)


def _join_from(doc_path: str, ref: str) -> str:
    if ref.startswith("/"):
        return _normalise(ref)
    slash = doc_path.rfind("/")
    directory = doc_path[: slash + 1] if slash >= 0 else ""
    return _normalise(f"{directory}{ref}")


def _resolve_against_chain(top_path: str | None, chain: list[str], ref: str) -> str:
    doc = _normalise(top_path or "")
    for link in chain:
        doc = _join_from(doc, link)
    return _join_from(doc, ref)


def _file_of(draft_id: str) -> str:
    return f"{ROOT_DIR}{draft_id}{SCREEN_SUFFIX}"


def _key_of(path: str) -> str | None:
    if not path.startswith(ROOT_DIR) or not path.endswith(SCREEN_SUFFIX):
        return None
    return path[len(ROOT_DIR) : len(path) - len(SCREEN_SUFFIX)]


@dataclass(frozen=True)
class _Resolver:
    """Loads an embed by the ref ``expandScreen`` hands its resolver (C§8 harness rules)."""

    store: DraftStore
    top_file: str
    top_on_path: bool

    def path_of(self, ref: str) -> str:
        """The root-relative path of a ref: as given on the path, else joined to the top's directory."""
        return ref if self.top_on_path else _join_from(self.top_file, ref)

    def key_of(self, ref: str) -> str | None:
        return _key_of(self.path_of(ref))

    def load(self, ref: str) -> dict[str, Any] | None:
        key = self.key_of(ref)
        if key is None:
            return None
        target = contained_file(self.store.root, _file_of(key))
        if target is None:
            return None
        try:
            doc = json.loads(target.read_bytes())
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


def _walk_mounted(root: Any, macros: Macros, hidden: bool):
    """``(raw node, substituted props, hidden)`` for every node the renderer walks, in document order."""

    def walk(node: Any, chain: list[Any], hidden: bool):
        if not isinstance(node, dict) or node.get("id") in chain:
            return
        raw_props = node.get("props")
        props = _substitute_deep(raw_props, macros) if isinstance(raw_props, dict) else {}
        hidden = hidden or props.get("visible") is False
        yield node, props, hidden
        if not _mounts_children(node.get("type")):
            return
        children = node.get("children")
        for child in children if isinstance(children, list) else []:
            yield from walk(child, [*chain, node.get("id")], hidden)

    yield from walk(root, [], hidden)


def _embed_slots(doc: dict[str, Any], macros: Macros, hidden: bool):
    """``(index, node, file, node macros, hidden)`` per embed that takes an index (the runtime's embedOrder)."""
    index = 0
    for node, props, node_hidden in _walk_mounted(doc.get("root"), macros, hidden):
        if node.get("type") != EMBED_TYPE:
            continue
        file = props.get("file")
        if not isinstance(file, str) or file == "":
            continue
        node_macros = props.get("macros")
        yield (
            index,
            node,
            _substitute(file, macros),
            node_macros if isinstance(node_macros, dict) else None,
            node_hidden,
        )
        index += 1


def _walk(
    doc: dict[str, Any],
    store: DraftStore,
    draft_id: str,
    macros: Macros | None,
    top_on_path: bool,
    pvs: set[str],
):
    top_file = _file_of(draft_id)
    screen_path = top_file if top_on_path else None
    resolver = _Resolver(store, top_file, top_on_path)
    provided = dict(macros or {})
    top_macros, top_empty = _resolve_screen_macros(doc, provided)

    def visit(instance: _Instance):
        current = instance.doc
        formulas = current.get("formulas")
        group = _Group(pvs, formulas, instance.macros)
        screen = draft_id if instance.key == "" else resolver.key_of(instance.screen_ref)
        for raw, props, hidden in _walk_mounted(current.get("root"), instance.macros, instance.hidden):
            ntype = raw.get("type")
            channels = _node_channels(ntype, props)
            rule_channels = _rule_channels(raw.get("rules"), formulas, instance.macros)
            raw_props = raw.get("props") if isinstance(raw.get("props"), dict) else {}
            unresolved = any(
                _is_unresolved(written, _substitute(written, instance.macros), instance.empty_defaults)
                for written in _strings(raw_props)
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
                "hidden": hidden,
            }
            yield _Visit(raw, node, instance.empty_defaults)
            for name in channels:
                group.add(name)
            group.rules(raw.get("rules"))

        if instance.depth + 1 > MAX_DEPTH:
            return
        children: list[_Instance] = []
        for index, node, file, node_macros, node_hidden in _embed_slots(current, instance.macros, instance.hidden):
            ref = _resolve_against_chain(screen_path, instance.chain, file)
            if ref in instance.ancestors:
                continue
            child = resolver.load(ref)
            if child is None:
                continue
            passed = _child_macros(instance.macros, node_macros)
            # A name stays empty-defaulted unless the embed sets it; a value the
            # embed authors non-empty that comes out empty took an empty default.
            authored = node.get("props", {}).get("macros") if isinstance(node.get("props"), dict) else None
            authored = authored if isinstance(authored, dict) else {}
            inherited = {name for name in instance.empty_defaults if name not in (node_macros or {})}
            inherited |= {
                name
                for name, value in authored.items()
                if isinstance(value, str) and value != "" and passed.get(name) == ""
            }
            child_macros, child_empty = _resolve_screen_macros(child, passed)
            children.append(
                _Instance(
                    doc=child,
                    macros=child_macros,
                    empty_defaults=inherited | child_empty,
                    depth=instance.depth + 1,
                    key=f"{instance.key}/{index}:{node.get('id')}",
                    screen_ref=ref,
                    chain=[*instance.chain, file],
                    ancestors=[*instance.ancestors, ref],
                    hidden=node_hidden,
                )
            )
        for child_instance in children:
            yield from visit(child_instance)

    yield from visit(
        _Instance(
            doc=doc,
            macros=top_macros,
            empty_defaults=top_empty,
            depth=0,
            key="",
            screen_ref=screen_path or "",
            chain=[],
            ancestors=[_resolve_against_chain(None, [], screen_path)] if screen_path else [],
            hidden=False,
        )
    )


def expand(
    doc: dict[str, Any],
    store: DraftStore,
    draft_id: str,
    *,
    macros: Macros | None = None,
    top_on_path: bool = False,
) -> list[ExpandedNode]:
    """Every node the renderer mounts for ``doc`` (stored as ``draft_id``), per screen instance.

    ``top_on_path=False`` is the viewer's mode: the top document is not on the
    cycle path and embed refs resolve against its directory. ``True`` puts the
    top on the path as ``screens/<draft_id>.screen.json`` (the vectors'
    ``screenPath``-given mode). ``macros`` are the caller's values, taken
    verbatim over the screen's defaults.
    """
    return [visit.node for visit in _walk(doc, store, draft_id, macros, top_on_path, set())]


def expanded_pvs(
    doc: dict[str, Any],
    store: DraftStore,
    draft_id: str,
    *,
    macros: Macros | None = None,
    top_on_path: bool = False,
) -> list[str]:
    """The PVs the runtime's collector lists for ``doc``, sorted (as JavaScript sorts) and unique.

    Prop channels of mounted, mapped nodes and rule PVs, trimmed; ``loc://``
    names, names with an unresolved macro and blanks dropped; a ``fox://`` name
    stands for the inputs of the formula it names (and of formulas those use),
    read from the declaring screen's formulas. Actions are not read.
    """
    pvs: set[str] = set()
    for _ in _walk(doc, store, draft_id, macros, top_on_path, pvs):
        pass
    return sorted(pvs, key=lambda name: name.encode("utf-16-be"))


def _raw_channels(ntype: Any, raw_props: dict[str, Any]) -> list[str]:
    if ntype == EMBED_TYPE or _is_unmapped(ntype):
        return []
    names: list[str] = []
    for key, value in raw_props.items():
        if key not in _SKIPPED_PROPS:
            _channels_of(value, key, names)
    return names


def list_write_bindings(store: DraftStore) -> list[WriteBinding]:
    """Every PV the stored screens can write, one row per binding.

    Each manifest entry is expanded as a top-level screen in the viewer's mode
    with no caller macros. A row is a ``write_pv`` action's ``pvName`` (``kind``
    ``write_pv``) or a channel of a write-capable widget (``kind`` its type, one
    of ``INERT_IN_OSPREY_TYPES``). ``pvResolved`` is the substituted, trimmed
    name; a blank one is no binding. ``unresolvedMacro`` is true when it still
    holds ``${`` or ``$(`` or a macro it uses took an empty default. A draft
    that cannot be read is skipped.
    """
    rows: list[WriteBinding] = []
    for entry in store.manifest()["screens"]:
        draft_id = entry["id"]
        try:
            doc, _rev = store.read(draft_id)
        except (LookupError, OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        for visit in _walk(doc, store, draft_id, None, False, set()):
            node, raw = visit.node, visit.raw
            raw_props = raw.get("props") if isinstance(raw.get("props"), dict) else {}
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
                rows.append(
                    {
                        "screen": node["screen"],
                        "instance": node["instance"],
                        "nodeId": node["nodeId"],
                        "kind": kind,
                        "pvAsWritten": pv,
                        "pvResolved": resolved,
                        "local": resolved.startswith(LOC_PREFIX),
                        "unresolvedMacro": _is_unresolved(pv, resolved, visit.empty_defaults),
                    }
                )
    return rows
