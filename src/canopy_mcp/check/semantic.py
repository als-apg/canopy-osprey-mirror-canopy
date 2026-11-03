"""Semantic checks the JSON Schema cannot express.

Ported from the SDK validator (``canopy-sdk-js`` ``screen-ir/src/validate.ts``)
and extended with the panel-builder codes. Every check tolerates malformed
input — anything of the wrong type is skipped, since the schema pass already
reports it.

Codes: ``macro_default``, ``unresolved_formula``, ``duplicate_node_id``,
``open_webpage`` and ``curve_channel`` are errors; ``write_not_relayed``,
``writes_to_machine``, ``warning``, ``unknown_widget`` and ``waveform_tab`` are
warnings. Draft-ref codes live in ``check.refs``.

A plot's ``props.curves[]`` entries bind their PVs under the key the renderer
reads (``y_channel`` on ``waveform-plot`` and ``xy-plot``, ``y_channels`` on
``pv-array-plot``); a curve without it is ``curve_channel``. This is checked
here rather than left to the staged prop schemas, which may not be staged at all.
A curve refused here draws no ``invalid_prop`` from the prop lint.

The hints that point at a viewer tool page (the Trace tab, its correlation
page, the Waveform tab) are given only for the tools the host serves, passed as
``tools`` (:func:`canopy_mcp.static.configured_tools`). A screen that is little
more than a ``waveform-plot`` is ``waveform_tab`` when the host serves the
``waveform`` page: one hint, on its first plot, when every plot of the screen
draws (a non-empty ``curves`` list with no ``curve_channel`` error) and every
other node is a container, a shape or a static label. A waveform plot beside
other controls is a screen.

A write-capable control or a ``write_pv`` action is ``write_not_relayed`` while
the server relays no writes (``relay_on=False``). With the PUT relay on it is
``writes_to_machine`` when it names at least one machine channel, that is a
non-blank channel that is neither ``loc://`` nor ``formula://`` (those stay in the
browser, so a control that names only them keeps ``write_not_relayed``).

This module is the one home of the node vocabulary the checks, the screen walk
and the push closure share: the channel prefixes, the action types and the
write-capable widget types.
"""

from __future__ import annotations

import re
from typing import Any, Literal, overload
from collections.abc import Iterator, Collection

from canopy_mcp.channels import (
    LOC_PREFIX,
    FORMULA_PREFIX,
    WRITE_PV_ACTION,
    WRITE_CAPABLE_TYPES,
    iter_channels,
)
from canopy_mcp.check.findings import Finding, node_id, pointer

WRITE_NOT_RELAYED_CODE = "write_not_relayed"
UNKNOWN_WIDGET_TYPE = "unknown-widget"
CURVE_CHANNEL_CODE = "curve_channel"
WAVEFORM_TAB_CODE = "waveform_tab"
WAVEFORM_PLOT_TYPE = "waveform-plot"

#: Node types that are not controls: containers, shapes and static labels. A
#: screen of waveform plots and nothing but these draws the ``waveform_tab`` hint.
PASSIVE_TYPES: frozenset[str] = frozenset(
    {
        "absolute-canvas",
        "arc",
        "ellipse",
        "frame",
        "group",
        "group-box",
        "image-view",
        "line",
        "polygon",
        "rectangle",
        "scroll-area",
        "text-label",
    }
)
_WAVEFORM_TAB_HINT = (
    "this screen is little more than a waveform-plot; a live view of array PVs, overlays or the delta "
    "to a reference is not a screen, it is the viewer's Waveform tab's job"
)

#: The key the renderer reads a plot curve's PV(s) from: ``y_channel`` on a
#: waveform or XY plot (one PV per curve), ``y_channels`` on a PV array plot
#: (one scalar PV per sample).
PLOT_CURVE_KEY: dict[str, str] = {
    "waveform-plot": "y_channel",
    "xy-plot": "y_channel",
    "pv-array-plot": "y_channels",
}
#: Keys a curve is written with by mistake; a finding names the ones it finds.
_CURVE_KEY_MISTAKES = ("channel", "pv", "pvName", "y_channel", "y_channels")
_CURVE_HINTS = {
    "waveform-plot": "a waveform-plot curve draws one array (waveform) PV named by y_channel",
    "xy-plot": "an xy-plot curve draws y_channel against x_channel",
    "pv-array-plot": "a pv-array-plot curve draws the scalar PVs listed in y_channels, one sample each",
}
#: Per plot type, the viewer tool that does what the plot is often mistaken for,
#: and the sentence that says so; added only when the host serves that tool.
_CURVE_TOOL_HINTS = {
    "waveform-plot": ("trace", "a time series of a scalar PV is not a widget, it is the Trace tab's job"),
    "xy-plot": (
        "correlation",
        "one scalar PV against another over time is not a widget, it is the Trace correlation page's job",
    ),
}

# http(s) absolute URLs and root-relative paths (what actionHost.openWindow allows).
_SAFE_URL = re.compile(r"^(?:https?://|/)", re.IGNORECASE)

Path = tuple[str | int, ...]


def check_semantic(doc: Any, *, relay_on: bool = False, tools: Collection[str] = ()) -> list[Finding]:
    """All semantic findings for ``doc``, in document order per check.

    ``relay_on`` says the server relays PV writes (``CANOPY_PUT_RELAY``): a
    control or ``write_pv`` action with a machine channel is then reported as
    ``writes_to_machine`` instead of ``write_not_relayed``. ``tools`` names the
    viewer tool pages the host serves; only those are named in hints.
    """
    if not isinstance(doc, dict):
        return []
    findings: list[Finding] = []
    findings += _macro_defaults(doc)
    nodes = list(iter_nodes(doc))
    findings += _duplicate_ids(nodes)
    findings += _formula_engine_refs(doc, nodes)
    for path, node in nodes:
        findings += _node_checks(path, node, relay_on)
        findings += _curve_channels(path, node, tools)
    if "waveform" in tools:
        findings += _waveform_tab(nodes)
    return findings


@overload
def iter_nodes(doc: Any) -> Iterator[tuple[Path, dict[str, Any]]]: ...
@overload
def iter_nodes(doc: Any, *, parents: Literal[True]) -> Iterator[tuple[Path, dict[str, Any], dict[str, Any] | None]]: ...


def iter_nodes(doc: Any, *, parents: bool = False) -> Iterator[tuple[Any, ...]]:
    """Depth-first ``(path, node)`` over ``root`` and its ``children``, in document order.

    With ``parents``, ``(path, node, parent node)``; the root's parent is ``None``.
    """
    root = doc.get("root") if isinstance(doc, dict) else None
    stack: list[tuple[Path, Any, dict[str, Any] | None]] = [(("root",), root, None)]
    while stack:
        path, node, parent = stack.pop()
        if not isinstance(node, dict):
            continue
        yield (path, node, parent) if parents else (path, node)
        children = node.get("children")
        if isinstance(children, list):
            for i in range(len(children) - 1, -1, -1):
                stack.append(((*path, "children", i), children[i], node))


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


def _formula_engine_name(ref: str) -> str:
    return ref[len(FORMULA_PREFIX) :] if ref.startswith(FORMULA_PREFIX) else ref


def _formula_engine_refs(doc: dict[str, Any], nodes: list[tuple[Path, dict[str, Any]]]) -> list[Finding]:
    formulas = doc.get("formulas")
    names = {
        f["name"]
        for f in (formulas if isinstance(formulas, list) else [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    }
    out: list[Finding] = []

    def require(ref: str, nid: str | None, where: Path, what: str) -> None:
        name = _formula_engine_name(ref)
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
                if isinstance(value, str) and value.startswith(FORMULA_PREFIX):
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
                if isinstance(name, str) and name.startswith(FORMULA_PREFIX):
                    require(name, nid, (*rpath, "pvs", p, "name"), f"rule '{rule.get('id')}' pv")
    return out


def is_machine_channel(name: Any) -> bool:
    """Whether ``name`` is a channel a write would reach the machine on (not blank, ``loc://`` or ``formula://``)."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    return bool(name) and not name.startswith((LOC_PREFIX, FORMULA_PREFIX))


def _node_checks(path: Path, node: dict[str, Any], relay_on: bool = False) -> list[Finding]:
    nid = node_id(node)
    ntype = node.get("type")
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    out: list[Finding] = []

    if isinstance(ntype, str) and ntype in WRITE_CAPABLE_TYPES:
        if relay_on and any(is_machine_channel(c) for _, c in iter_channels(ntype, props)):
            code, message = "writes_to_machine", f"'{ntype}' writes to the machine"
        else:
            code, message = (
                WRITE_NOT_RELAYED_CODE,
                f"'{ntype}' writes to the machine; it is inert while this server relays no writes",
            )
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
                code, message = WRITE_NOT_RELAYED_CODE, "write_pv action is inert while this server relays no writes"
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


def _waveform_tab(nodes: list[tuple[Path, dict[str, Any]]]) -> list[Finding]:
    """The ``waveform_tab`` hint: a screen of drawing waveform plots and nothing but passive nodes."""
    plots = [(path, node) for path, node in nodes if node.get("type") == WAVEFORM_PLOT_TYPE]
    if not plots:
        return []
    if any(not _is_passive(node.get("type")) for _, node in nodes if node.get("type") != WAVEFORM_PLOT_TYPE):
        return []
    for path, node in plots:
        props = node.get("props")
        curves = props.get("curves") if isinstance(props, dict) else None
        if not isinstance(curves, list) or not curves or _curve_findings(path, node):
            return []
    path, node = plots[0]
    return [Finding(node_id(node), WAVEFORM_TAB_CODE, pointer((*path, "type")), "warning", _WAVEFORM_TAB_HINT)]


def _is_passive(ntype: Any) -> bool:
    return isinstance(ntype, str) and ntype in PASSIVE_TYPES


def _curve_channels(path: Path, node: dict[str, Any], tools: Collection[str]) -> list[Finding]:
    """``curve_channel`` errors: each curve of a plot names its PV(s) under the key the renderer reads."""
    return [finding for _, finding in _curve_findings(path, node, tools)]


def refused_curves(node: dict[str, Any]) -> frozenset[int]:
    """Indices into ``props.curves`` of the curves of ``node`` that draw a ``curve_channel`` error."""
    return frozenset(c for c, _ in _curve_findings((), node))


def _curve_findings(path: Path, node: dict[str, Any], tools: Collection[str] = ()) -> list[tuple[int, Finding]]:
    """The ``curve_channel`` errors of ``node``, each with the index of its curve, in curve order."""
    ntype = node.get("type")
    key = PLOT_CURVE_KEY.get(ntype) if isinstance(ntype, str) else None
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    curves = props.get("curves") if key is not None else None
    if key is None or not isinstance(curves, list):
        return []
    nid = node_id(node)
    hint = _CURVE_HINTS[ntype]
    tool, tool_hint = _CURVE_TOOL_HINTS.get(ntype, (None, ""))
    if tool in tools:
        hint = f"{hint}; {tool_hint}"
    out: list[tuple[int, Finding]] = []
    for c, curve in enumerate(curves):
        where = (*path, "props", "curves", c)
        if not isinstance(curve, dict):
            message = f"curve {c} is not an object; {hint}"
            out.append((c, Finding(nid, CURVE_CHANNEL_CODE, pointer(where), "error", message)))
            continue
        if _curve_binds(curve.get(key), key):
            x_channel = curve.get("x_channel")
            if key == "y_channel" and x_channel is not None and not isinstance(x_channel, str):
                out.append(
                    (
                        c,
                        Finding(
                            nid,
                            CURVE_CHANNEL_CODE,
                            pointer((*where, "x_channel")),
                            "error",
                            f"curve {c}: x_channel must be a PV name or null",
                        ),
                    )
                )
            continue
        mistakes = [k for k in _CURVE_KEY_MISTAKES if k != key and k in curve]
        found = f" (found {', '.join(repr(k) for k in mistakes)} instead)" if mistakes else ""
        message = f"curve {c} names no {key}{found}; {hint}"
        out.append((c, Finding(nid, CURVE_CHANNEL_CODE, pointer((*where, key)), "error", message)))
    return out


def _curve_binds(value: Any, key: str) -> bool:
    """Whether ``value`` binds ``key``: a non-blank name, or for ``y_channels`` a non-empty list of them."""
    if key == "y_channels":
        return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)
    return isinstance(value, str) and bool(value.strip())
