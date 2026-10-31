"""What a stored screen can write, and what it takes along when it is pushed to the library.

Both read the screen walk of :mod:`canopy_mcp.screens`:

- :func:`list_screen_write_bindings` lists every PV one stored screen can
  write; :func:`list_write_bindings` does so for every screen of a store. The
  library report shows these rows to the reviewer of a push.
- :func:`push_closure` lists the files a screen needs once it is copied
  elsewhere, and refuses a screen whose refs would not survive the copy or
  whose rules retarget what it loads, does or binds
  (:func:`retargeting_rules`).

Stdlib only, besides ``canopy_mcp.store``, ``canopy_mcp.check``,
``canopy_mcp.channels`` and ``canopy_mcp.screens``.
"""

from __future__ import annotations

import re
import json
from typing import Any, TypedDict
from collections.abc import Iterator, Collection

from canopy_mcp.store import ID_PATTERN, DraftStore
from canopy_mcp.screens import (
    visits,
    props_of,
    has_macro,
    substitute,
    raw_channels,
    is_unresolved,
)
from canopy_mcp.channels import (
    LOC_PREFIX,
    CHANNEL_KEY,
    WRITE_PV_ACTION,
    WRITE_CAPABLE_TYPES,
)
from canopy_mcp.check.refs import (
    IMAGE_PROPS,
    Tree,
    Resolved,
    resolve,
    dir_path,
    iter_refs,
    screen_id,
    screen_path,
    iter_image_refs,
)
from canopy_mcp.check.semantic import iter_nodes

__all__ = [
    "RULE_REF_PROPS",
    "PushRefused",
    "Refusal",
    "WriteBinding",
    "list_screen_write_bindings",
    "list_write_bindings",
    "push_closure",
    "retargeting_rules",
]


# --- write bindings -------------------------------------------------------------------


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


def _written_names(raw: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """``(kind, pv as written)`` for every write one node names: each channel of a
    write-capable widget and each ``write_pv`` action's ``pvName``."""
    ntype = raw.get("type")
    raw_props = props_of(raw)
    if ntype in WRITE_CAPABLE_TYPES:
        yield from ((ntype, name) for name in raw_channels(ntype, raw_props))
    actions = raw_props.get("actions")
    for action in actions if isinstance(actions, list) else []:
        if isinstance(action, dict) and action.get("type") == WRITE_PV_ACTION and isinstance(action.get("pvName"), str):
            yield WRITE_PV_ACTION, action["pvName"]


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
    of ``WRITE_CAPABLE_TYPES``). ``top`` is ``draft_id``; ``screen`` and
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
    for visit in visits(doc, store, draft_id, include_hidden=True, listing=listing):
        node = visit.node
        for kind, pv in _written_names(visit.raw):
            resolved = substitute(pv, node["macros"]).strip()
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
                "unresolvedMacro": is_unresolved(pv, resolved, visit.empty_defaults),
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
    if any(CHANNEL_KEY.fullmatch(name) for name in names):
        return "rule_retargets_channel"
    return None


def retargeting_rules(doc: dict[str, Any]):
    """``(node path, node, rule index, targetProperty, code)`` for every rule of ``doc`` a push refuses."""
    for path, node in iter_nodes(doc):
        rules = node.get("rules")
        for index, rule in enumerate(rules if isinstance(rules, list) else []):
            target = rule.get("targetProperty") if isinstance(rule, dict) else None
            code = _rule_retargets(target) if isinstance(target, str) else None
            if code is not None:
                yield path, node, index, target, code


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
    if has_macro(ref):
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
    source = dir_path(source_dir)
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

        for _path, node, _index, target, code in retargeting_rules(doc):
            if code == "rule_retargets_ref":
                message = f"a rule retargets {target!r}, which names what the screen loads, does or binds"
            else:
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
