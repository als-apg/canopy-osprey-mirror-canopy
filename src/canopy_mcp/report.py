"""The report of a screen tree: what its screens can write, run, open and load.

``python -m canopy_mcp.report <root> [--served-at personal|library] (--emit|--write|--check)``

``<root>`` is a store root, the directory that holds ``screens/``. The report is
the review record of a screen library: ``WRITE-BINDINGS.json`` and, as tables,
``WRITE-BINDINGS.md``, written beside ``screens/``.

- ``--emit`` prints one JSON object ``{"json": <report>, "md": <markdown>}`` on
  stdout and writes nothing.
- ``--write`` writes both files under an ``flock`` on ``<root>``, the markdown
  first and the JSON last. A ``provenance`` object in the JSON already there is
  carried over unchanged.
- ``--check`` recomputes everything and compares: the stored JSON without
  ``provenance`` and without ``generator.canopy_version``, and the markdown
  byte for byte. It exits non-zero on a mismatch or a refusal. A missing root
  passes (``no library``), and so does a root with no files and no report
  (``empty library, no report``).

``--served-at`` says where the viewer serves the root, which decides the two ref
lists below. It defaults to ``library``: a report file exists for a tree that is
served as one.

**The document** (:func:`report_for_tree`, schema ``canopy-report/1``):

- ``generator``: ``{schema, canopy_version}``.
- ``files``: ``{digest, list}``, the listing of ``canopy_mcp.listing``.
- ``manifest``: the ids of the top-level screens, sorted.
- ``templates``: every ``*.screen.json`` file with its sha256, the macros it
  declares and references, and its role: ``manifest`` (a top-level screen),
  ``embedded`` (reached only as an embed of one) or ``unlisted`` (neither, such
  as a dotted name the id rule drops).
- ``screens``: per manifest entry, expanded as the viewer mounts it with no
  caller macros: its ``instances``, the macros a URL can set (``urlMacros``),
  the embeds and ``open_display`` files that did not resolve
  (``unresolvedRefs``) and the nodes left with an unresolved macro.
- ``writeBindings``: every PV a screen can write, with ``top``.
- ``executeActions``, ``commandButtons``, ``openDisplays``: every ``execute``
  action, every button that can hold one, and every ``open_display`` action.
- ``absoluteUrls``: per document, every ``open_webpage`` URL, every image the
  browser fetches by URL instead of from the tree, and every other string of a
  node that starts with ``http://`` or ``https://``.
- ``crossRootRefs``, ``rootRelativeRefs``: the resolver's ``cross_root_ref``
  findings, and its ``root_relative_ref`` findings when the tree is served as a
  library (in a personal store that one is a warning of ``check``).
- ``ruleRetargetedChannels``, ``ruleRetargetedRefs``: per document, every rule
  whose ``targetProperty`` a push refuses.

``provenance`` is not written here. A caller may add it to the JSON; it is in no
comparison and never rendered. The report carries no digest of its own rows.

**Hidden nodes** count everywhere: every walk runs with ``include_hidden=True``,
since a rule can show a statically hidden widget, and rows say ``hidden``.

**The filesystem is asked once.** The tree is scanned, the listing judged, and
every screen read by the path the listing spells. From there a ref resolves only
when its path is a member of the listing as that exact string, so a report
written on a case-insensitive host equals the one a case-sensitive host
recomputes.

**Refusals.** A screen holding a string with a forbidden character
(``canopy_mcp.store.is_forbidden_char``) refuses every mode with
``invalid_text: <file> <json pointer>`` before any digest is computed.
``--check`` also refuses a listing refusal, any ``.svg`` and a non-empty
``crossRootRefs``, ``rootRelativeRefs``, ``ruleRetargetedChannels`` or
``ruleRetargetedRefs``; ``--emit`` and ``--write`` print those as warnings.

**The markdown** is computed from ``generator.schema``, ``files`` and the rows
alone, and every table cell goes through :func:`safe_cell`.

Stdlib only, besides ``canopy_mcp.store``, ``canopy_mcp.listing``,
``canopy_mcp.check`` and ``canopy_mcp.screens``.
"""

from __future__ import annotations

import re
import sys
import json
import hashlib
import argparse
from typing import Any
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Sequence
from importlib.metadata import PackageNotFoundError, version

from canopy_mcp import screens
from canopy_mcp.store import (
    SCREEN_SUFFIX,
    ReadOnlyStore,
    lock_root,
    write_atomic,
    is_forbidden_char,
    first_forbidden_pointer,
)
from canopy_mcp.listing import REPORT_NAMES, Refusal, scan, judge, refusal_text
from canopy_mcp.check.refs import (
    LIBRARY,
    ROOT_DIR,
    IMAGE_PROPS,
    OPEN_DISPLAY_ACTION,
    ServedAt,
    BufferTree,
    resolve,
    screen_id,
    check_refs,
    screen_key,
    screen_path,
)
from canopy_mcp.check.findings import pointer
from canopy_mcp.check.semantic import iter_nodes

__all__ = [
    "REPORT_JSON",
    "REPORT_MD",
    "ROW_KEYS",
    "SCHEMA",
    "ReportRefused",
    "main",
    "render_markdown",
    "report_for_tree",
    "safe_cell",
]

SCHEMA = "canopy-report/1"
REPORT_JSON, REPORT_MD = REPORT_NAMES
#: The distribution whose version ``generator.canopy_version`` records.
DISTRIBUTION = "canopy"
#: Every key of the document that holds rows: all but ``generator``, ``files`` and ``provenance``.
ROW_KEYS = (
    "manifest",
    "templates",
    "screens",
    "writeBindings",
    "executeActions",
    "commandButtons",
    "openDisplays",
    "absoluteUrls",
    "crossRootRefs",
    "rootRelativeRefs",
    "ruleRetargetedChannels",
    "ruleRetargetedRefs",
)
#: The row lists ``--check`` requires to be empty.
REFUSED_ROW_KEYS = ("crossRootRefs", "rootRelativeRefs", "ruleRetargetedChannels", "ruleRetargetedRefs")

#: The node types whose actions run a command on the host.
COMMAND_BUTTON_TYPES = ("regular-button", "shell-command-button")
EXECUTE_ACTION = "execute"
OPEN_WEBPAGE_ACTION = "open_webpage"
#: Query parameters the viewer keeps for itself; never a macro.
RESERVED_URL_PARAMS = frozenset({"embedded", "theme", "mode"})
RESERVED_URL_PREFIX = "_canopy"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_REFUSED = 3

_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
_MARKUP = re.compile(r"[\\|<>\[\]`*_]")
_FOLDED = str.maketrans({"\r": " ", "\n": " ", "\t": " "})


class ReportRefused(ValueError):
    """The tree cannot be reported as it stands; the message is the refusal line."""


# --- small readers --------------------------------------------------------------------


def _printable(value: Any) -> str:
    """``value`` on one ASCII line: control and non-ASCII characters as backslash escapes."""
    return str(value).encode("unicode_escape").decode("ascii")


def _macro_names(value: Any) -> set[str]:
    """Every macro name referenced in a string anywhere inside ``value``."""
    return {name for _parts, text in _strings(value, ()) for name in screens._macro_names(text)}


def _declared_macros(doc: Any) -> list[dict[str, Any]]:
    declared = doc.get("macros") if isinstance(doc, dict) else None
    out = []
    for decl in declared if isinstance(declared, list) else []:
        if isinstance(decl, dict) and isinstance(decl.get("name"), str):
            default = decl.get("default")
            out.append({"name": decl["name"], "default": default if isinstance(default, str) else None})
    return out


def _url_macro(name: str) -> bool:
    return name not in RESERVED_URL_PARAMS and not name.startswith(RESERVED_URL_PREFIX)


def _parse(data: bytes) -> tuple[Any, str | None]:
    """The decoded JSON value and ``None``, or ``None`` and a fixed error text.

    The text names no parser message, so it is the same under every Python.
    """
    try:
        return json.loads(data), None
    except (ValueError, RecursionError):
        return None, "not valid JSON"


def _strings(value: Any, parts: tuple[str | int, ...]):
    """``(path, string)`` for every string inside ``value``, in document order."""
    if isinstance(value, str):
        yield parts, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*parts, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*parts, index))


def _at(doc: Any, path: str) -> Any:
    """The value at RFC 6901 pointer ``path`` in ``doc``, or ``None`` when there is none."""
    here = doc
    for token in path.split("/")[1:]:
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(here, list) and key.isdigit() and int(key) < len(here):
            here = here[int(key)]
        elif isinstance(here, dict) and key in here:
            here = here[key]
        else:
            return None
    return here


# --- rows read from one document --------------------------------------------------------


def _absolute_urls(screen: str, doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, node in iter_nodes(doc):
        ntype = node.get("type")
        props = node.get("props")
        actions = props.get("actions") if isinstance(props, dict) else None
        image_props = IMAGE_PROPS.get(ntype, ()) if isinstance(ntype, str) else ()
        for name in ("props", "rules"):
            for parts, text in _strings(node.get(name), (name,)):
                kind = None
                if len(parts) == 4 and parts[:2] == ("props", "actions") and parts[3] == "url":
                    action = actions[parts[2]] if isinstance(actions, list) else None
                    if isinstance(action, dict) and action.get("type") == OPEN_WEBPAGE_ACTION and text:
                        kind = OPEN_WEBPAGE_ACTION
                elif len(parts) == 2 and name == "props" and parts[1] in image_props:
                    if text and resolve(ROOT_DIR, (), text, kind="image").external:
                        kind = "image"
                if kind is None and _HTTP_URL.match(text):
                    kind = "string"
                if kind is not None:
                    rows.append(
                        {
                            "screen": screen,
                            "nodeId": node.get("id"),
                            "nodeType": ntype,
                            "kind": kind,
                            "path": pointer((*path, *parts)),
                            "url": text,
                        }
                    )
    return rows


def _rule_retargets(screen: str, doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rules of ``doc`` a push refuses: ``(channel rows, ref rows)``."""
    channels: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for path, node, index, target, code in screens._retargeting_rules(doc):
        row = {
            "screen": screen,
            "nodeId": node.get("id"),
            "nodeType": node.get("type"),
            "path": pointer((*path, "rules", index, "targetProperty")),
            "targetProperty": target,
        }
        (refs if code == "rule_retargets_ref" else channels).append(row)
    return channels, refs


# --- the report -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    report: dict[str, Any]
    listing_refusals: tuple[Refusal, ...]


def _canopy_version() -> str:
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "unknown"


def _is_embed_of(key: str, instance: str, node_id: Any) -> bool:
    """Whether instance ``key`` is the one the embed node ``node_id`` of ``instance`` mounts."""
    head, tail = f"{instance}/", f":{node_id}"
    if not key.startswith(head) or not key.endswith(tail):
        return False
    slot = key[len(head) : len(key) - len(tail)]
    return slot == "h" or slot.isdigit()


def _url_macros(
    draft_id: str, doc: dict[str, Any], instances: dict[str, dict[str, Any]], doc_of: Callable[[str], Any]
) -> list[dict[str, Any]]:
    """The macros a URL may set for the top screen ``draft_id``: declared by it or referenced under it."""
    referenced: dict[str, set[str]] = {}
    for screen in dict.fromkeys(row["screen"] for row in instances.values()):
        for name in _macro_names(doc if screen == draft_id else doc_of(screen)):
            referenced.setdefault(name, set()).add(screen)
    declared = {decl["name"]: decl["default"] for decl in _declared_macros(doc)}
    return [
        {
            "name": name,
            "default": declared.get(name),
            "declared": name in declared,
            "referencedIn": sorted(referenced.get(name, ())),
        }
        for name in sorted(set(declared) | set(referenced))
        if _url_macro(name)
    ]


def _node_rows(
    draft_id: str,
    nodes: list[screens.ExpandedNode],
    instances: dict[str, dict[str, Any]],
    served_at: ServedAt,
    listing: frozenset[str],
    rows: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add the action and command-button rows of ``nodes`` to ``rows``; returns the unresolved refs and macros."""
    unresolved_refs: list[dict[str, Any]] = []
    unresolved_macros: list[dict[str, Any]] = []
    for node in nodes:
        where = {"screen": node["screen"], "instance": node["instance"], "nodeId": node["nodeId"]}
        under_top = {"topScreen": draft_id, **where, "nodeType": node["type"]}
        props = node["props"]
        file = props.get("file")
        if node["type"] == screens.EMBED_TYPE and isinstance(file, str) and file != "":
            if not any(_is_embed_of(key, node["instance"], node["nodeId"]) for key in instances):
                unresolved_refs.append({"kind": "embed", **where, "file": file})
        actions = props.get("actions")
        for action in actions if isinstance(actions, list) else []:
            if not isinstance(action, dict):
                continue
            if action.get("type") == EXECUTE_ACTION:
                rows["executeActions"].append(
                    {
                        **under_top,
                        "command": action.get("command"),
                        "description": action.get("description"),
                        "hidden": node["hidden"],
                    }
                )
            elif action.get("type") == OPEN_DISPLAY_ACTION and isinstance(action.get("file"), str):
                found = resolve(
                    f"{ROOT_DIR}{node['screen']}{SCREEN_SUFFIX}",
                    (),
                    action["file"],
                    served_at=served_at,
                    listing=listing,
                )
                rows["openDisplays"].append(
                    {**under_top, "file": action["file"], "target": found.draft_id, "hidden": node["hidden"]}
                )
                if not found.ok:
                    unresolved_refs.append({"kind": OPEN_DISPLAY_ACTION, **where, "file": action["file"]})
        if node["type"] in COMMAND_BUTTON_TYPES:
            rows["commandButtons"].append({**under_top, "hidden": node["hidden"]})
        if node["unresolvedMacro"]:
            unresolved_macros.append(where)
    return unresolved_refs, unresolved_macros


def _ref_rows(
    draft_id: str,
    doc: dict[str, Any],
    tree: BufferTree,
    served_at: ServedAt,
    listing: frozenset[str],
    doc_of: Callable[[str], Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """``(row key, row)`` for every cross-root ref under ``draft_id``, and every root-relative one in a library."""
    for finding in check_refs(doc, tree, draft_id, served_at=served_at, listing=listing):
        found_at = finding.to_dict()
        if finding.code == "cross_root_ref":
            key = "crossRootRefs"
        elif finding.code == "root_relative_ref" and served_at == LIBRARY:
            key = "rootRelativeRefs"
        else:
            continue
        screen = found_at.get("docId") or draft_id
        yield (
            key,
            {
                "topScreen": draft_id,
                "screen": screen,
                "nodeId": found_at["nodeId"],
                "kind": found_at.get("kind"),
                "path": found_at["path"],
                "ref": _at(doc if screen == draft_id else doc_of(screen), found_at["path"]),
            },
        )


def _build(root: Path, served_at: ServedAt) -> _Built:
    entries = scan(root)
    buffers: dict[str, bytes] = {}
    docs: dict[str, tuple[Any, str | None]] = {}
    for entry in entries:
        path = entry["path"]
        if entry["kind"] != "file" or screen_key(path) is None:
            continue
        data = (root / path).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ReportRefused(f"changed_while_read: {_printable(path)} changed while the report was computed")
        doc, error = _parse(data)
        found = first_forbidden_pointer(doc)
        if found is not None:
            raise ReportRefused(f"invalid_text: {_printable(path)} {_printable(found)}")
        buffers[path] = data
        docs[path] = (doc, error if error else None if isinstance(doc, dict) else "not a JSON object")

    judged = judge(entries)
    listing = judged.paths
    tree = BufferTree({path: data for path, data in buffers.items() if path in listing})
    manifest_ids = sorted(filter(None, map(screen_id, listing)))

    def doc_of(screen: str) -> Any:
        path = f"{ROOT_DIR}{screen}{SCREEN_SUFFIX}"
        return docs[path][0] if path in listing and path in docs else None

    reached: set[str] = set()
    screens_out: list[dict[str, Any]] = []
    rows: dict[str, list[dict[str, Any]]] = {key: [] for key in ROW_KEYS[3:]}
    for draft_id in manifest_ids:
        top_path = screen_path(draft_id)
        doc, error = docs[top_path]
        entry: dict[str, Any] = {"id": draft_id, "path": top_path}
        if error is not None:
            entry["error"] = error
            screens_out.append(entry)
            continue
        nodes = screens.expand(doc, tree, draft_id, include_hidden=True, listing=listing)
        instances: dict[str, dict[str, Any]] = {}
        for node in nodes:
            instances.setdefault(
                node["instance"], {"instance": node["instance"], "screen": node["screen"], "hidden": node["hidden"]}
            )
        reached.update(row["screen"] for key, row in instances.items() if key != "")

        unresolved_refs, unresolved_macros = _node_rows(draft_id, nodes, instances, served_at, listing, rows)
        entry.update(
            {
                "instances": list(instances.values()),
                "urlMacros": _url_macros(draft_id, doc, instances, doc_of),
                "unresolvedRefs": unresolved_refs,
                "unresolvedMacros": unresolved_macros,
            }
        )
        screens_out.append(entry)

        rows["writeBindings"] += [
            dict(row) for row in screens.list_screen_write_bindings(tree, draft_id, listing=listing)
        ]
        for key, row in _ref_rows(draft_id, doc, tree, served_at, listing, doc_of):
            rows[key].append(row)

    manifest_set = set(manifest_ids)
    templates: list[dict[str, Any]] = []
    for path, sha in judged.files:
        screen = screen_key(path)
        if screen is None:
            continue
        doc, error = docs[path]
        template: dict[str, Any] = {
            "path": path,
            "screen": screen,
            "role": "manifest" if screen in manifest_set else "embedded" if screen in reached else "unlisted",
            "sha256": sha,
            "declaredMacros": _declared_macros(doc),
            "referencedMacros": sorted(_macro_names(doc)),
        }
        templates.append(template)
        if error is not None:
            template["error"] = error
            continue
        rows["absoluteUrls"] += _absolute_urls(screen, doc)
        channels, refs = _rule_retargets(screen, doc)
        rows["ruleRetargetedChannels"] += channels
        rows["ruleRetargetedRefs"] += refs

    report = {
        "generator": {"schema": SCHEMA, "canopy_version": _canopy_version()},
        "files": {
            "digest": judged.digest,
            "list": [{"path": path, "sha256": sha} for path, sha in judged.files],
        },
        "manifest": manifest_ids,
        "templates": templates,
        "screens": screens_out,
        **rows,
    }
    return _Built(report, judged.refusals)


def report_for_tree(root: Path | str, *, served_at: ServedAt) -> dict[str, Any]:
    """The ``canopy-report/1`` document over the store root ``root`` (see the module text).

    ``served_at`` is ``"personal"`` or ``"library"``. The document has no
    ``provenance``. Raises :class:`ReportRefused` for a screen that holds a
    forbidden character, and :class:`OSError` when the root cannot be read.
    """
    return _build(Path(root), served_at).report


def _refusals(built: _Built) -> list[str]:
    """Every reason ``--check`` refuses the tree, one printable line each."""
    report = built.report
    out = [refusal_text(refusal) for refusal in built.listing_refusals]
    out += [
        f"svg: {_printable(file['path'])}: SVG is markup a browser runs; delete it or convert it to a raster image"
        for file in report["files"]["list"]
        if file["path"].lower().endswith(".svg")
    ]
    for key in REFUSED_ROW_KEYS:
        for row in report[key]:
            what = row["ref"] if "ref" in row else row["targetProperty"]
            out.append(
                f"{key}: screen {_printable(row['screen'])} node {_printable(row['nodeId'])} "
                f"at {_printable(row['path'])}: {_printable(what)}"
            )
    return out


# --- markdown -------------------------------------------------------------------------


def safe_cell(value: Any) -> str:
    """``value`` as the text of one markdown table cell, ASCII only.

    ``None`` is empty and a boolean ``yes`` or ``no``. Every character the
    forbidden-character predicate names is stripped; CR, LF and TAB fold to a
    space; ``\\``, ``|`` and the markup characters ``<``, ``>``, ``[``, ``]``,
    `````, ``*`` and ``_`` get a backslash; what is left of non-ASCII is written
    as ``\\uXXXX`` (UTF-16 code units, as JSON writes them).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = "".join(char for char in str(value) if not is_forbidden_char(char)).translate(_FOLDED)
    text = _MARKUP.sub(lambda match: "\\" + match.group(0), text)
    if text.isascii():
        return text
    units = text.encode("utf-16-be")
    return "".join(
        chr(unit) if unit < 0x80 else f"\\u{unit:04x}"
        for unit in (int.from_bytes(units[at : at + 2], "big") for at in range(0, len(units), 2))
    )


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["none", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(safe_cell(v) for v in row) + " |" for row in rows]
    return [*out, ""]


def _section(title: str, headers: list[str], rows: list[list[Any]]) -> list[str]:
    return [f"## {title} ({len(rows)})", "", *_table(headers, rows)]


def _where(row: dict[str, Any]) -> list[Any]:
    return [row["topScreen"], row["screen"], row["instance"] or "(top)", row["nodeId"], row["nodeType"]]


def render_markdown(report: dict[str, Any]) -> str:
    """``WRITE-BINDINGS.md``: the report as tables, write bindings first.

    Reads ``generator.schema``, ``files`` and the ``ROW_KEYS`` rows, nothing else.
    """
    files = report["files"]
    lines = [
        "# Write bindings of the screen library",
        "",
        "Generated by `python -m canopy_mcp.report`; the same data is in",
        f"`{REPORT_JSON}`. Every PV a stored screen can write is listed under *Write bindings*.",
        "",
        "## Sources",
        "",
        f"- schema: `{safe_cell(report['generator']['schema'])}`",
        f"- library digest: `{safe_cell(files['digest'])}` ({len(files['list'])} files)",
        "",
        *_section(
            "Write bindings",
            [
                "top screen",
                "screen",
                "instance",
                "node",
                "kind",
                "PV as written",
                "PV resolved",
                "local",
                "unresolved macro",
            ],
            [
                [
                    r["top"],
                    r["screen"],
                    r["instance"] or "(top)",
                    r["nodeId"],
                    r["kind"],
                    r["pvAsWritten"],
                    r["pvResolved"],
                    r["local"],
                    r["unresolvedMacro"],
                ]
                for r in report["writeBindings"]
            ],
        ),
        *_section(
            "Execute actions",
            ["top screen", "screen", "instance", "node", "type", "command", "description", "hidden"],
            [[*_where(r), r["command"], r["description"], r["hidden"]] for r in report["executeActions"]],
        ),
        *_section(
            "Command buttons",
            ["top screen", "screen", "instance", "node", "type", "hidden"],
            [[*_where(r), r["hidden"]] for r in report["commandButtons"]],
        ),
        *_section(
            "Open displays",
            ["top screen", "screen", "instance", "node", "type", "file", "target", "hidden"],
            [[*_where(r), r["file"], r["target"], r["hidden"]] for r in report["openDisplays"]],
        ),
        *_section(
            "Absolute URLs",
            ["screen", "node", "type", "kind", "path", "URL"],
            [[r["screen"], r["nodeId"], r["nodeType"], r["kind"], r["path"], r["url"]] for r in report["absoluteUrls"]],
        ),
    ]
    for title, key in (("Cross-root refs", "crossRootRefs"), ("Root-relative refs", "rootRelativeRefs")):
        lines += _section(
            title,
            ["top screen", "screen", "node", "kind", "path", "ref"],
            [[r["topScreen"], r["screen"], r["nodeId"], r["kind"], r["path"], r["ref"]] for r in report[key]],
        )
    for title, key in (
        ("Rule-retargeted channels", "ruleRetargetedChannels"),
        ("Rule-retargeted refs", "ruleRetargetedRefs"),
    ):
        lines += _section(
            title,
            ["screen", "node", "type", "path", "target property"],
            [[r["screen"], r["nodeId"], r["nodeType"], r["path"], r["targetProperty"]] for r in report[key]],
        )
    lines += ["## Top-level screens", ""]
    for screen in report["screens"]:
        lines += [f"### {safe_cell(screen['id'])}", "", f"- path: {safe_cell(screen['path'])}"]
        if "error" in screen:
            lines += [f"- not readable: {safe_cell(screen['error'])}", ""]
            continue
        lines += [f"- instances: {len(screen['instances'])}", ""]
        lines += ["Instances:", ""]
        lines += _table(
            ["instance", "screen", "hidden"],
            [[i["instance"] or "(top)", i["screen"], i["hidden"]] for i in screen["instances"]],
        )
        lines += ["URL-settable macros:", ""]
        lines += _table(
            ["name", "default", "declared", "referenced in"],
            [[m["name"], m["default"], m["declared"], ", ".join(m["referencedIn"])] for m in screen["urlMacros"]],
        )
        lines += ["Unresolved refs:", ""]
        lines += _table(
            ["kind", "screen", "instance", "node", "file"],
            [
                [r["kind"], r["screen"], r["instance"] or "(top)", r["nodeId"], r["file"]]
                for r in screen["unresolvedRefs"]
            ],
        )
        lines += ["Nodes with an unresolved macro:", ""]
        lines += _table(
            ["screen", "instance", "node"],
            [[r["screen"], r["instance"] or "(top)", r["nodeId"]] for r in screen["unresolvedMacros"]],
        )
    lines += ["## Templates", ""]
    lines += _table(
        ["path", "role", "declared macros", "referenced macros", "sha256"],
        [
            [
                t["path"],
                t["role"] + (" (not readable)" if "error" in t else ""),
                ", ".join(f"{m['name']}={m['default'] or ''}" for m in t["declaredMacros"]),
                ", ".join(t["referencedMacros"]),
                t["sha256"],
            ]
            for t in report["templates"]
        ],
    )
    lines += ["## Files", ""]
    lines += _table(["sha256", "path"], [[f["sha256"], f["path"]] for f in files["list"]])
    return "\n".join(lines)


# --- the command ----------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m canopy_mcp.report",
        description="Report what the screens of a store root can write, run, open and load.",
    )
    parser.add_argument("root", type=Path, help="the store root: the directory that holds screens/")
    parser.add_argument(
        "--served-at",
        choices=("personal", LIBRARY),
        default=LIBRARY,
        help=f"where the viewer serves the root (default {LIBRARY})",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit", action="store_true", help='print {"json": <report>, "md": <markdown>} on stdout')
    mode.add_argument("--write", action="store_true", help=f"write {REPORT_JSON} and {REPORT_MD} into the root")
    mode.add_argument("--check", action="store_true", help="recompute and compare with the files in the root")
    return parser.parse_args(argv)


def _say(message: str) -> None:
    print(f"canopy_mcp.report: {message}", file=sys.stderr)


def _json_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _stored(root: Path) -> Any:
    """The decoded ``WRITE-BINDINGS.json`` of ``root``, or ``None`` when there is none to read."""
    try:
        return json.loads((root / REPORT_JSON).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


def _comparable(report: Any) -> Any:
    """``report`` without what ``--check`` does not compare: ``provenance`` and ``generator.canopy_version``."""
    if not isinstance(report, dict):
        return report
    out = {key: value for key, value in report.items() if key != "provenance"}
    if isinstance(out.get("generator"), dict):
        out["generator"] = {key: value for key, value in out["generator"].items() if key != "canopy_version"}
    return out


def _warn_refusals(built: _Built) -> None:
    """Print what ``--check`` would refuse, as warnings: ``--emit`` and ``--write`` go ahead regardless."""
    for line in _refusals(built):
        _say(f"warning: --check refuses this tree: {line}")


def _emit(root: Path, served_at: ServedAt) -> int:
    built = _build(root, served_at)
    _warn_refusals(built)
    document = {"json": built.report, "md": render_markdown(built.report)}
    sys.stdout.write(json.dumps(document, ensure_ascii=True) + "\n")
    return EXIT_OK


def _write(root: Path, served_at: ServedAt) -> int:
    with lock_root(root):
        built = _build(root, served_at)
        report = dict(built.report)
        stored = _stored(root)
        if isinstance(stored, dict) and "provenance" in stored:
            report["provenance"] = stored["provenance"]
        # The JSON is what a freshness check reads, so it lands last.
        write_atomic(root / REPORT_MD, render_markdown(built.report).encode("ascii"))
        write_atomic(root / REPORT_JSON, _json_bytes(report))
    _warn_refusals(built)
    print(f"wrote {REPORT_JSON} and {REPORT_MD} ({len(built.report['files']['list'])} files)")
    return EXIT_OK


def _check(root: Path, served_at: ServedAt) -> int:
    if not root.exists():
        print("no library")
        return EXIT_OK
    built = _build(root, served_at)
    report = built.report
    has_report = (root / REPORT_JSON).exists() or (root / REPORT_MD).exists()
    if not report["files"]["list"] and not built.listing_refusals and not has_report:
        print("empty library, no report")
        return EXIT_OK

    refusals = _refusals(built)
    for line in refusals:
        _say(f"refused: {line}")

    differs: list[str] = []
    stored = _stored(root)
    if stored is None:
        differs.append(f"{REPORT_JSON} is missing or not readable")
    else:
        mine, theirs = _comparable(report), _comparable(stored)
        if mine != theirs:
            keys = [
                key
                for key in dict.fromkeys([*mine, *(theirs if isinstance(theirs, dict) else ())])
                if not isinstance(theirs, dict) or mine.get(key) != theirs.get(key)
            ]
            differs.append(f"{REPORT_JSON} differs in: {', '.join(_printable(key) for key in keys)}")
    try:
        if (root / REPORT_MD).read_bytes() != render_markdown(report).encode("ascii"):
            differs.append(f"{REPORT_MD} differs")
    except OSError:
        differs.append(f"{REPORT_MD} is missing or not readable")
    for line in differs:
        _say(f"stale: {line}")
    if differs:
        _say(
            f"the report was recomputed with --served-at {served_at}; after reviewing the tree, regenerate it with: "
            f"python -m canopy_mcp.report <root> --served-at {served_at} --write"
        )
    if refusals:
        return EXIT_REFUSED
    if differs:
        return EXIT_MISMATCH
    print(f"report is current ({len(report['files']['list'])} files, --served-at {served_at})")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run = _emit if args.emit else _write if args.write else _check
    try:
        return run(args.root, args.served_at)
    except ReportRefused as exc:
        _say(f"refused: {exc}")
        return EXIT_REFUSED
    except (OSError, ReadOnlyStore) as exc:
        _say(f"cannot read or write {_printable(args.root)}: {_printable(exc)}")
        return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
