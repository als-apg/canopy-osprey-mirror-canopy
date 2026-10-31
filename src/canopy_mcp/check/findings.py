"""The finding record every check pass returns, and JSON-pointer helpers.

A finding names the node it concerns (``nodeId``), a stable ``code``, the
JSON-pointer ``path`` into the checked document, a ``severity`` and a human
``message``. Write tools diff findings on ``(nodeId, code, path)``, so the path
is always a deterministic RFC 6901 pointer built from the document's keys and
indices.
"""

from __future__ import annotations

from typing import Any, Literal
from dataclasses import dataclass
from collections.abc import Iterable, Sequence

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem found in a Screen-IR document.

    ``node_id`` is ``None`` when the path lies outside the widget tree
    (``metadata``, ``macros``, ``formulas``, top-level keys). ``doc_id`` is set
    only for a node of an embedded document; ``original_class`` only on
    ``unknown_widget``.
    """

    node_id: str | None
    code: str
    path: str
    severity: Severity
    message: str
    doc_id: str | None = None
    original_class: str | None = None

    @property
    def key(self) -> tuple[str | None, str, str]:
        """The identity write tools diff on."""
        return (self.node_id, self.code, self.path)

    def to_dict(self) -> dict[str, Any]:
        """The camelCase JSON shape returned to the agent; optional keys only when set."""
        out: dict[str, Any] = {
            "nodeId": self.node_id,
            "code": self.code,
            "path": self.path,
            "severity": self.severity,
            "message": self.message,
        }
        if self.doc_id is not None:
            out["docId"] = self.doc_id
        if self.original_class is not None:
            out["originalClass"] = self.original_class
        return out


def pointer(parts: Iterable[str | int]) -> str:
    """RFC 6901 JSON pointer for ``parts`` (``[]`` is the whole document, ``""``)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def node_id_at(doc: Any, parts: Sequence[str | int]) -> str | None:
    """Id of the nearest widget node enclosing the location ``parts`` in ``doc``.

    Walks ``root`` then ``children[i]`` steps; the deepest node on the way with
    a string ``id`` wins. Paths that do not start at ``root`` have no node.
    """
    if not isinstance(doc, dict) or not parts or parts[0] != "root":
        return None
    node: Any = doc.get("root")
    found = node_id(node)
    i = 1
    while i + 1 < len(parts) and parts[i] == "children" and isinstance(parts[i + 1], int):
        children = node.get("children") if isinstance(node, dict) else None
        idx = parts[i + 1]
        if not isinstance(children, list) or not 0 <= idx < len(children):
            break
        node = children[idx]
        found = node_id(node) or found
        i += 2
    return found


def node_id(node: Any) -> str | None:
    """A widget node's ``id`` when it is a string, else ``None``."""
    if isinstance(node, dict) and isinstance(node.get("id"), str):
        return node["id"]
    return None
