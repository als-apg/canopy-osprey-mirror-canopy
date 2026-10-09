"""Semantic checks the JSON Schema cannot express.

Ported from the SDK validator (``canopy-sdk-js`` ``screen-ir/src/validate.ts``)
and extended with the panel-builder codes. Every check tolerates malformed
input — anything of the wrong type is skipped, since the schema pass already
reports it.

Codes: ``macro_default``, ``unresolved_formula``, ``duplicate_node_id`` and
``open_webpage`` are errors; ``inert_in_osprey``, ``writes_to_machine``,
``warning`` and ``unknown_widget`` are warnings. Draft-ref codes live in
``check.refs``.

A write-capable control or a ``write_pv`` action is ``inert_in_osprey`` while
the server relays no writes (``relay_on=False``). With the PUT relay on it is
``writes_to_machine`` when it names at least one machine channel, that is a
non-blank channel that is neither ``loc://`` nor ``fox://`` (those stay in the
browser, so a control that names only them keeps ``inert_in_osprey``).
"""

from __future__ import annotations

import re
from typing import Any
from collections.abc import Iterator

from canopy_mcp.check.findings import Finding, node_id, pointer

FOX_PREFIX = "fox://"
LOC_PREFIX = "loc://"
WRITE_PV_ACTION = "write_pv"
UNKNOWN_WIDGET_TYPE = "unknown-widget"

#: Control types that write to the machine; OSPREY embeds are read-only, so
#: these render but do nothing there.
INERT_IN_OSPREY_TYPES: frozenset[str] = frozenset(
    {
        "pv-button",
        "pv-toggle",
        "pv-slider",
        "pv-text-input",
        "pv-checkbox",
        "pv-enum-combobox",
        "pv-spinbox",
        "pv-radio-group",
    }
)

# http(s) absolute URLs and root-relative paths (what actionHost.openWindow allows).
_SAFE_URL = re.compile(r"^(?:https?://|/)", re.IGNORECASE)

Path = tuple[str | int, ...]


def check_semantic(doc: Any, *, relay_on: bool = False) -> list[Finding]:
    """All semantic findings for ``doc``, in document order per check.

    ``relay_on`` says the server relays PV writes (``CANOPY_PUT_RELAY``): a
    control or ``write_pv`` action with a machine channel is then reported as
    ``writes_to_machine`` instead of ``inert_in_osprey``.
    """
    if not isinstance(doc, dict):
        return []
    findings: list[Finding] = []
    findings += _macro_defaults(doc)
    nodes = list(iter_nodes(doc))
    findings += _duplicate_ids(nodes)
    findings += _fox_refs(doc, nodes)
    for path, node in nodes:
        findings += _node_checks(path, node, relay_on)
    return findings


def iter_nodes(doc: Any) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Depth-first ``(path, node)`` over ``root`` and its ``children``."""
    root = doc.get("root") if isinstance(doc, dict) else None
    stack: list[tuple[Path, Any]] = [(("root",), root)]
    while stack:
        path, node = stack.pop()
        if not isinstance(node, dict):
            continue
        yield path, node
        children = node.get("children")
        if isinstance(children, list):
            for i in range(len(children) - 1, -1, -1):
                stack.append(((*path, "children", i), children[i]))


def _macro_defaults(doc: dict[str, Any]) -> list[Finding]:
    kind = doc.get("kind") or "screen"
    macros = doc.get("macros")
    out: list[Finding] = []
    if not isinstance(macros, list):
        return out
    for i, macro in enumerate(macros):
        if not isinstance(macro, dict):
            continue
        name = macro.get("name")
        has_default = macro.get("default") is not None
        if kind == "screen" and not has_default:
            msg = f"macro '{name}' needs a default on a screen (kind='screen')"
        elif kind == "template" and has_default:
            msg = f"macro '{name}' must not declare a default on a template"
        else:
            continue
        out.append(Finding(None, "macro_default", pointer(("macros", i)), "error", msg))
    return out


def _duplicate_ids(nodes: list[tuple[Path, dict[str, Any]]]) -> list[Finding]:
    seen: dict[str, Path] = {}
    out: list[Finding] = []
    for path, node in nodes:
        nid = node_id(node)
        if nid is None:
            continue
        if nid in seen:
            out.append(
                Finding(
                    nid,
                    "duplicate_node_id",
                    pointer((*path, "id")),
                    "error",
                    f"node id '{nid}' is already used at {pointer(seen[nid])}",
                )
            )
        else:
            seen[nid] = path
    return out


def _fox_name(ref: str) -> str:
    return ref[len(FOX_PREFIX) :] if ref.startswith(FOX_PREFIX) else ref


def _fox_refs(doc: dict[str, Any], nodes: list[tuple[Path, dict[str, Any]]]) -> list[Finding]:
    formulas = doc.get("formulas")
    names = {
        f["name"]
        for f in (formulas if isinstance(formulas, list) else [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    }
    out: list[Finding] = []

    def require(ref: str, nid: str | None, where: Path, what: str) -> None:
        name = _fox_name(ref)
        if name not in names:
            out.append(
                Finding(
                    nid,
                    "unresolved_formula",
                    pointer(where),
                    "error",
                    f"{what} references formula '{name}' but no formulas[] entry declares it",
                )
            )

    for path, node in nodes:
        nid = node_id(node)
        props = node.get("props")
        if isinstance(props, dict):
            for key, value in props.items():
                if isinstance(value, str) and value.startswith(FOX_PREFIX):
                    require(value, nid, (*path, "props", key), f"prop '{key}'")
        rules = node.get("rules")
        if not isinstance(rules, list):
            continue
        for r, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            rpath = (*path, "rules", r)
            if isinstance(rule.get("formula"), str):
                require(rule["formula"], nid, (*rpath, "formula"), f"rule '{rule.get('id')}'")
            pvs = rule.get("pvs")
            for p, pv in enumerate(pvs if isinstance(pvs, list) else []):
                name = pv.get("name") if isinstance(pv, dict) else None
                if isinstance(name, str) and name.startswith(FOX_PREFIX):
                    require(name, nid, (*rpath, "pvs", p, "name"), f"rule '{rule.get('id')}' pv")
    return out


def is_machine_channel(name: Any) -> bool:
    """Whether ``name`` is a channel a write would reach the machine on (not blank, ``loc://`` or ``fox://``)."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    return bool(name) and not name.startswith((LOC_PREFIX, FOX_PREFIX))


def _control_channels(ntype: Any, props: dict[str, Any]) -> list[str]:
    # The same channel reading as the write bindings (canopy_mcp.screens), so
    # the check and the sidecar's write rows agree on what a control writes.
    # Imported here: canopy_mcp.screens imports this module.
    from canopy_mcp.screens import _raw_channels

    return _raw_channels(ntype, props)


def _node_checks(path: Path, node: dict[str, Any], relay_on: bool = False) -> list[Finding]:
    nid = node_id(node)
    ntype = node.get("type")
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    out: list[Finding] = []

    if ntype in INERT_IN_OSPREY_TYPES:
        if relay_on and any(is_machine_channel(c) for c in _control_channels(ntype, props)):
            code, message = "writes_to_machine", f"'{ntype}' writes to the machine"
        else:
            code, message = "inert_in_osprey", f"'{ntype}' writes to the machine; it is inert when embedded in OSPREY"
        out.append(Finding(nid, code, pointer((*path, "type")), "warning", message))

    actions = props.get("actions")
    for a, action in enumerate(actions if isinstance(actions, list) else []):
        if not isinstance(action, dict):
            continue
        apath = (*path, "props", "actions", a)
        atype = action.get("type")
        if atype == WRITE_PV_ACTION:
            if relay_on and is_machine_channel(action.get("pvName")):
                code, message = "writes_to_machine", "write_pv action writes to the machine"
            else:
                code, message = "inert_in_osprey", "write_pv action is inert when embedded in OSPREY"
            out.append(Finding(nid, code, pointer(apath), "warning", message))
        elif atype == "open_webpage":
            url = action.get("url")
            if not isinstance(url, str) or not _SAFE_URL.match(url.strip()):
                out.append(
                    Finding(
                        nid,
                        "open_webpage",
                        pointer((*apath, "url")),
                        "error",
                        f"open_webpage URL {url!r} is not http(s) or a root-relative path",
                    )
                )

    warnings = node.get("warnings")
    for w, text in enumerate(warnings if isinstance(warnings, list) else []):
        out.append(Finding(nid, "warning", pointer((*path, "warnings", w)), "warning", str(text)))

    if ntype == UNKNOWN_WIDGET_TYPE:
        original = props.get("originalClass")
        original = original if isinstance(original, str) else None
        out.append(
            Finding(
                nid,
                "unknown_widget",
                pointer((*path, "type")),
                "warning",
                f"widget class {original!r} has no Screen-IR equivalent; placeholder rendered",
                original_class=original,
            )
        )
    return out
