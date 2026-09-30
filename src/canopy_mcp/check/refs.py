"""Draft-ref resolution: ``unresolved_ref`` and ``nested_ref_base``.

A screen names other screens in two places: the ``file`` prop of an
``embedded-display`` node and the ``file`` of an ``open_display`` action. The
viewer resolves every such ref against the directory of the TOP screen
document (``api.ts`` ``makeResolveScreen``; nested chains reuse the same base),
so this pass does the same: against ``dirname(draft_id)`` in the draft store,
or the store root when no id is given.

Classification looks at the RAW ref first -- a leading ``/``, ``~`` or
``scheme:`` is ``absolute`` -- and only a relative ref is normalized (``.``
dropped, ``..`` folded as ``actionHost.normalizePath`` does). ``above_root`` is
decided after joining with the base directory, when a leading ``..`` survives.
The result must be ``<draft id>.screen.json`` with a valid draft id, else
``invalid_id``; a valid id with no stored draft is ``missing``.

Embedded documents are followed (recursively, cycle-safe) ONLY to compute these
two codes; nothing else is collected from them. A ref inside an embedded
document that resolves to a different target under per-file resolution (its
own directory) than under the viewer's top-document rule is reported as
``nested_ref_base``, which then stands in for an ``unresolved_ref`` on the same
ref; a nested ref both rules agree on is reported as ``unresolved_ref`` when it
does not resolve. Findings on nodes of an embedded document carry ``doc_id``.

Image refs (``image-view.src``, ``pv-button.onImage``/``offImage``) are checked
too, as ``unresolved_ref`` warnings with ``kind: 'image'`` (screen refs carry
``kind: 'screen'``). An ``http(s):``, ``data:`` or ``blob:`` ref (any case) or
one starting with ``/`` is external and skipped; any other ``scheme:`` is
``invalid_ref``. A relative ref resolves against the top document's directory
for the top document and against ``dirname(doc_id)`` inside a followed
embedded document (never ``nested_ref_base``); a leading ``..`` left after
normalizing is ``above_root``, a path the store could not hold is
``invalid_ref``, and one the viewer's static route does not serve
(``store.resolve_static``) is ``missing``.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from dataclasses import dataclass

from canopy_mcp.store import (
    ID_PATTERN,
    SCREEN_SUFFIX,
    IMAGE_SUFFIXES,
    DraftStore,
    DraftNotFound,
    asset_relpath_reason,
)
from canopy_mcp.check.findings import Finding, node_id, pointer
from canopy_mcp.check.semantic import iter_nodes

Reason = Literal["missing", "absolute", "above_root", "invalid_id", "invalid_ref"]
ImageStatus = Literal["external", "above_root", "invalid_ref"]
RefKind = Literal["screen", "image"]

EMBEDDED_DISPLAY_TYPE = "embedded-display"
OPEN_DISPLAY_ACTION = "open_display"
IMAGE_PROPS: dict[str, tuple[str, ...]] = {"image-view": ("src",), "pv-button": ("onImage", "offImage")}

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_EXTERNAL_IMAGE = re.compile(r"^(https?|data|blob):", re.IGNORECASE)

Path = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RefFinding(Finding):
    """A :class:`Finding` that also carries the ``unresolved_ref`` reason and ref kind."""

    reason: Reason | None = None
    kind: RefKind | None = None

    def to_dict(self) -> dict[str, Any]:
        out = Finding.to_dict(self)
        if self.reason is not None:
            out["reason"] = self.reason
        if self.kind is not None:
            out["kind"] = self.kind
        return out


@dataclass(frozen=True, slots=True)
class Resolution:
    """Where a ref lands: a draft id (``reason`` is ``None``) or why it does not."""

    draft_id: str | None
    reason: Reason | None

    @property
    def target(self) -> str:
        return self.draft_id if self.draft_id is not None else f"<{self.reason}>"


def dirname(draft_id: str | None) -> str:
    """Directory part of a draft id (``""`` for a top-level id or no id)."""
    if not draft_id or "/" not in draft_id:
        return ""
    return draft_id.rsplit("/", 1)[0]


def is_absolute(ref: str) -> bool:
    """A raw ref with a leading ``/``, ``~`` or ``scheme:``."""
    return ref.startswith(("/", "~")) or bool(_SCHEME.match(ref))


def normalize(path: str) -> list[str]:
    """Segments of ``path`` with ``""``/``.`` dropped and ``..`` folded; unmatched ``..`` survive at the front."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." and out and out[-1] != "..":
            out.pop()
        else:
            out.append(seg)
    return out


def resolve_ref(ref: str, base_dir: str) -> tuple[str | None, Reason | None]:
    """Classify ``ref`` against ``base_dir`` without touching the store.

    Returns ``(draft_id, None)`` for a well-formed ref, else ``(None, reason)``
    with reason ``absolute``, ``above_root`` or ``invalid_id``.
    """
    if is_absolute(ref):
        return None, "absolute"
    segments = normalize(f"{base_dir}/{ref}" if base_dir else ref)
    if segments and segments[0] == "..":
        return None, "above_root"
    joined = "/".join(segments)
    if not joined.endswith(SCREEN_SUFFIX):
        return None, "invalid_id"
    draft_id = joined[: -len(SCREEN_SUFFIX)]
    if not ID_PATTERN.fullmatch(draft_id):
        return None, "invalid_id"
    return draft_id, None


def resolve_image_ref(ref: str, base_dir: str) -> tuple[str | None, ImageStatus | None]:
    """Classify an image ``ref`` against ``base_dir`` without touching the store.

    Returns ``(relpath, None)`` for a storable ref, where ``relpath`` is in
    draft-id space (served as ``screens/<relpath>``), else ``(None, status)``
    with status ``external`` (fetched by the browser, not checked),
    ``invalid_ref`` or ``above_root``.
    """
    if _EXTERNAL_IMAGE.match(ref) or ref.startswith("/"):
        return None, "external"
    if _SCHEME.match(ref):
        return None, "invalid_ref"
    segments = normalize(f"{base_dir}/{ref}" if base_dir else ref)
    if segments and segments[0] == "..":
        return None, "above_root"
    relpath = "/".join(segments)
    if asset_relpath_reason(relpath) is not None:
        return None, "invalid_ref"
    return relpath, None


def check_refs(doc: Any, store: DraftStore, draft_id: str | None = None) -> list[Finding]:
    """``unresolved_ref`` / ``nested_ref_base`` warnings for ``doc`` checked at ``draft_id``.

    ``doc`` is the document being checked (it need not be the stored copy);
    ``draft_id`` names the directory its refs resolve in (store root when ``None``).
    """
    if not isinstance(doc, dict):
        return []
    if draft_id is not None:
        DraftStore.validate_id(draft_id)  # raises InvalidDraftId
    walker = _Walker(store, dirname(draft_id))
    if draft_id is not None:
        walker.visited.add(draft_id)
    walker.walk(doc, None)
    return walker.findings


class _Walker:
    def __init__(self, store: DraftStore, top_dir: str):
        self.store = store
        self.top_dir = top_dir
        self.visited: set[str] = set()
        self.findings: list[Finding] = []
        self._exists: dict[str, bool] = {}

    def resolve(self, ref: str, base_dir: str) -> Resolution:
        draft_id, reason = resolve_ref(ref, base_dir)
        if draft_id is not None and not self.exists(draft_id):
            return Resolution(None, "missing")
        return Resolution(draft_id, reason)

    def exists(self, draft_id: str) -> bool:
        if draft_id not in self._exists:
            self._exists[draft_id] = self.store.path_for(draft_id).is_file()
        return self._exists[draft_id]

    def walk(self, doc: dict[str, Any], doc_id: str | None) -> None:
        """Report ``doc``'s refs, then follow its embedded refs (``doc_id`` is ``None`` for the top document)."""
        follow: list[str] = []
        for nid, path, ref, embedded in iter_refs(doc):
            viewer = self.resolve(ref, self.top_dir)
            if doc_id is not None:
                per_file = self.resolve(ref, dirname(doc_id))
                if per_file != viewer:
                    self.findings.append(
                        RefFinding(
                            nid,
                            "nested_ref_base",
                            pointer(path),
                            "warning",
                            f"ref {ref!r} in embedded screen {doc_id!r} resolves to {viewer.target} against the "
                            f"top screen's directory (as the viewer does) but to {per_file.target} against its own",
                            doc_id=doc_id,
                        )
                    )
                    if embedded and viewer.draft_id is not None:
                        follow.append(viewer.draft_id)
                    continue
            if viewer.reason is not None:
                self.findings.append(
                    RefFinding(
                        nid,
                        "unresolved_ref",
                        pointer(path),
                        "warning",
                        _unresolved_message(ref, viewer.reason, self.top_dir),
                        doc_id=doc_id,
                        reason=viewer.reason,
                        kind="screen",
                    )
                )
            elif embedded and viewer.draft_id is not None:
                follow.append(viewer.draft_id)

        image_base = self.top_dir if doc_id is None else dirname(doc_id)
        for nid, path, ref in iter_image_refs(doc):
            relpath, status = resolve_image_ref(ref, image_base)
            if status == "external":
                continue
            reason: Reason | None = status
            if relpath is not None and self.store.resolve_static(f"screens/{relpath}") is None:
                reason = "missing"
            if reason is not None:
                self.findings.append(
                    RefFinding(
                        nid,
                        "unresolved_ref",
                        pointer(path),
                        "warning",
                        _image_message(ref, reason, image_base, relpath),
                        doc_id=doc_id,
                        reason=reason,
                        kind="image",
                    )
                )

        for child_id in follow:
            if child_id in self.visited:
                continue
            self.visited.add(child_id)
            try:
                child, _ = self.store.read(child_id)
            except (DraftNotFound, OSError, ValueError):
                continue  # unreadable embedded screens are reported by check(child_id)
            if isinstance(child, dict):
                self.walk(child, child_id)


def _unresolved_message(ref: str, reason: Reason, base_dir: str) -> str:
    where = f"'{base_dir}/'" if base_dir else "the drafts root"
    return {
        "missing": f"ref {ref!r} names no stored draft (resolved against {where})",
        "absolute": f"ref {ref!r} is absolute; screen refs must be relative to the top screen's directory",
        "above_root": f"ref {ref!r} climbs above the drafts root (resolved against {where})",
        "invalid_id": f"ref {ref!r} does not name a valid '<draft id>{SCREEN_SUFFIX}'",
    }[reason]


def _image_message(ref: str, reason: Reason, base_dir: str, relpath: str | None) -> str:
    where = f"'{base_dir}/'" if base_dir else "the drafts root"
    if reason == "missing":
        return (
            f"image {ref!r} is not in the draft store (looked for screens/{relpath}). convert_legacy copies "
            "images found beside the legacy file it converts (put the file there and re-port with "
            "overwrite=true); a hand-written screen must reference the image by URL."
        )
    if reason == "above_root":
        return (
            f"image {ref!r} climbs above the drafts root (resolved against {where}); an image must live under "
            "the drafts root (`screens/`): a relative ref may climb with `..` only as far as the store's top "
            "level, exactly as screen refs may — fix the ref, or reference it by URL"
        )
    scheme = _SCHEME.match(ref)
    scheme_text = f"`{scheme.group(0)}`" if scheme else "`<scheme>:`"
    return (
        f"image {ref!r} is not a valid asset path: each segment may use letters, digits, space, '.', '_' and "
        f"'-', the file must end in one of {', '.join(sorted(IMAGE_SUFFIXES))}, macros are not expanded, and a "
        f"scheme a browser cannot fetch ({scheme_text}; Phoebus `platform:` icons are not shipped) is refused "
        "— fix the ref, or reference it by http(s) URL"
    )


def iter_image_refs(doc: dict[str, Any]) -> list[tuple[str | None, Path, str]]:
    """``(node_id, path, ref)`` for every string image ref (``image-view.src``, ``pv-button.onImage/offImage``)."""
    out: list[tuple[str | None, Path, str]] = []
    if not isinstance(doc, dict):
        return out
    for path, node in iter_nodes(doc):
        ntype = node.get("type")
        names = IMAGE_PROPS.get(ntype) if isinstance(ntype, str) else None
        props = node.get("props")
        if not names or not isinstance(props, dict):
            continue
        for name in names:
            ref = props.get(name)
            if isinstance(ref, str) and ref:
                out.append((node_id(node), (*path, "props", name), ref))
    return out


def iter_refs(doc: dict[str, Any]) -> list[tuple[str | None, Path, str, bool]]:
    """``(node_id, path, ref, is_embedded)`` for every string screen ref, in document order."""
    out: list[tuple[str | None, Path, str, bool]] = []
    for path, node in iter_nodes(doc):
        nid = node_id(node)
        props = node.get("props")
        if not isinstance(props, dict):
            continue
        file = props.get("file")
        if node.get("type") == EMBEDDED_DISPLAY_TYPE and isinstance(file, str):
            out.append((nid, (*path, "props", "file"), file, True))
        actions = props.get("actions")
        for a, action in enumerate(actions if isinstance(actions, list) else []):
            if isinstance(action, dict) and action.get("type") == OPEN_DISPLAY_ACTION:
                target = action.get("file")
                if isinstance(target, str):
                    out.append((nid, (*path, "props", "actions", a, "file"), target, False))
    return out
