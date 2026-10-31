"""The one ref resolver, the tree it reads through, and the ref findings of ``check``.

A screen names other files in three places: the ``file`` prop of an
``embedded-display`` node, the ``file`` of an ``open_display`` action, and the
image props (``image-view.src``, ``pv-button.onImage``/``offImage``). Every
consumer -- ``check``, the screen walk, ``request_push`` and the report --
resolves them here.

**Store-path space.** A path is relative to one store root and uses ``/``:
the screen ``a/top`` is ``screens/a/top.screen.json``. A ref is folded through
its *chain*, the embed ``file`` refs that lead from the top document to the
document declaring it, exactly as the runtime's ``resolveRef`` does
(:func:`fold_ref`): each link is joined to the directory of the document before
it, ``.`` dropped and ``..`` folded, and a leading ``/`` starts over (the
runtime strips it). The viewer joins the result to the top document's
directory, so a ref resolves in the directory of the document that declares it.

**Roots.** A personal root is served at ``screens/...`` and a library root at
``library/screens/...`` (``served_at``). The path a ref's URL lands on decides:
inside this root's ``screens/`` it has a store path; in the other root it is a
``cross_root_ref``; anywhere else, or above everything, it is ``above_root``.

**Existence.** With a ``listing`` (a set of store paths), a ref exists only when
its path is a member, compared as an exact string, and the filesystem is never
asked -- so the answer is the same on a case-insensitive host. Without one, a
:class:`StoreTree` asks its store and any other tree answers from ``paths``.

**Findings** (:func:`check_refs`):

* ``unresolved_ref`` (warning) with a ``reason``. For a screen ref: ``absolute``
  (the raw ref starts with ``/``, ``~`` or ``scheme:``, decided before any
  folding), ``above_root``, ``invalid_id`` (not ``<draft id>.screen.json``) or
  ``missing``. For an image ref: ``above_root``, ``invalid_ref`` (a path the
  store could not hold, or a scheme a browser cannot fetch) or ``missing``; an
  ``http(s):``, ``data:`` or ``blob:`` ref (any case) or one starting with ``/``
  is external and skipped.
* ``cross_root_ref`` (error): the ref lands in the other root.
* ``root_relative_ref`` (warning here; push and the library report refuse it): the
  folded screen ref starts with ``/``, ``screens/`` or ``library/``. The viewer
  matches such a ref against its manifest before the declaring directory, so
  it stops meaning the same file once the screen moves.

Embedded documents are followed (recursively, cycle-safe) only to compute
these codes; nothing else is collected from them. Findings on nodes of an
embedded document carry ``doc_id``.
"""

from __future__ import annotations

import re
import json
from typing import Any, Literal, Protocol
from pathlib import Path as FilePath
from dataclasses import field, dataclass
from collections.abc import Mapping, Sequence, Collection

from canopy_mcp.store import (
    LIBRARY,
    ID_PATTERN,
    SCREEN_SUFFIX,
    IMAGE_SUFFIXES,
    DraftStore,
    contained_file,
    asset_relpath_reason,
)
from canopy_mcp.channels import EMBED_TYPE, OPEN_DISPLAY_ACTION
from canopy_mcp.check.findings import Finding, node_id, pointer
from canopy_mcp.check.semantic import iter_nodes

Reason = Literal["missing", "absolute", "above_root", "invalid_id", "invalid_ref"]
RefKind = Literal["screen", "image"]
ServedAt = Literal["personal", "library"]

IMAGE_PROPS: dict[str, tuple[str, ...]] = {"image-view": ("src",), "pv-button": ("onImage", "offImage")}

ROOT_DIR = "screens/"
PERSONAL: ServedAt = "personal"
ROOT_RELATIVE_PREFIXES = ("/", ROOT_DIR, f"{LIBRARY}/")

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_EXTERNAL_IMAGE = re.compile(r"^(https?|data|blob):", re.IGNORECASE)

Path = tuple[str | int, ...]


# --- the tree -------------------------------------------------------------------------


class Tree(Protocol):
    """What the resolver and the walk read through: store paths and their bytes."""

    @property
    def paths(self) -> frozenset[str]: ...

    def read(self, path: str) -> bytes:
        """The bytes at ``path``; :class:`FileNotFoundError` when the tree holds no such file."""
        ...


class StoreTree:
    """The tree of one :class:`DraftStore`: the screens and images under its ``screens/``.

    A file is in the tree when the viewer could be served it: a clean path under
    ``screens/``, not under a first segment the store reserves, with a screen or
    image suffix, and a regular file inside the root once symlinks are followed.
    With ``record=True`` every byte string :meth:`read` returns is kept in
    ``recorded`` by path, so a caller can run again on exactly those bytes
    (``BufferTree(tree.recorded)``).
    """

    def __init__(self, store: DraftStore, *, record: bool = False):
        self.store = store
        self.recorded: dict[str, bytes] | None = {} if record else None
        self._has: dict[str, bool] = {}
        self._paths: frozenset[str] | None = None

    def _file(self, path: str) -> FilePath | None:
        parts = path.split("/")
        if "\\" in path or "\x00" in path or len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
            return None
        if parts[0] != ROOT_DIR[:-1] or parts[1] in self.store.reserved:
            return None
        name = parts[-1].lower()
        if not (name.endswith(SCREEN_SUFFIX) or FilePath(name).suffix in IMAGE_SUFFIXES):
            return None
        return contained_file(self.store.root, FilePath(*parts))

    def has(self, path: str) -> bool:
        """Whether the store holds ``path`` (asks the filesystem, once per path)."""
        if path not in self._has:
            self._has[path] = self._file(path) is not None
        return self._has[path]

    @property
    def paths(self) -> frozenset[str]:
        """Every path of the tree, as spelled on disk (scanned on first use)."""
        if self._paths is None:
            found = []
            if self.store.screens.is_dir():
                for file in self.store.screens.rglob("*"):
                    path = file.relative_to(self.store.root).as_posix()
                    if self._file(path) is not None:
                        found.append(path)
            self._paths = frozenset(found)
        return self._paths

    def read(self, path: str) -> bytes:
        file = self._file(path)
        if file is None:
            raise FileNotFoundError(path)
        data = file.read_bytes()
        if self.recorded is not None:
            self.recorded[path] = data
        return data


class BufferTree:
    """A tree over byte strings held in memory, keyed by store path."""

    def __init__(self, buffers: Mapping[str, bytes]):
        self._buffers = dict(buffers)
        self.paths = frozenset(self._buffers)

    def read(self, path: str) -> bytes:
        try:
            return self._buffers[path]
        except KeyError:
            raise FileNotFoundError(path) from None


def exists(tree: Tree | None, path: str, listing: Collection[str] | None = None) -> bool:
    """Whether ``path`` exists: by exact-string membership of ``listing`` when one is given, else in ``tree``."""
    if listing is not None:
        return path in listing
    if isinstance(tree, StoreTree):
        return tree.has(path)
    return tree is not None and path in tree.paths


def screen_path(draft_id: str | None) -> str:
    """The store path of screen ``draft_id``; the root directory ``screens/`` when no id is given."""
    return f"{ROOT_DIR}{draft_id}{SCREEN_SUFFIX}" if draft_id else ROOT_DIR


def screen_key(path: str) -> str | None:
    """The name of a screen file below ``screens/``, without the id rule (a dotted name is a key)."""
    if not path.startswith(ROOT_DIR) or not path.endswith(SCREEN_SUFFIX):
        return None
    return path[len(ROOT_DIR) : -len(SCREEN_SUFFIX)]


def screen_id(path: str) -> str | None:
    """The draft id a store path names, or ``None`` when it is not ``screens/<draft id>.screen.json``."""
    draft_id = screen_key(path)
    return draft_id if draft_id is not None and ID_PATTERN.fullmatch(draft_id) else None


def dir_path(directory: str) -> str:
    """The store path of the directory ``directory`` (draft-id space, ``""`` for the top level), with a final ``/``."""
    return f"{ROOT_DIR}{directory}/" if directory else ROOT_DIR


def read_screen(tree: Tree, draft_id: str) -> Any:
    """The parsed screen ``draft_id`` of ``tree``; :class:`FileNotFoundError` or :class:`ValueError` when it has none."""
    return json.loads(tree.read(screen_path(draft_id)))


# --- folding --------------------------------------------------------------------------


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


def normalize_path(path: str) -> str:
    """``path`` normalized (:func:`normalize`) and joined again."""
    return "/".join(normalize(path))


def join_ref(doc_path: str, ref: str) -> str:
    """``ref`` joined to the directory of ``doc_path``; a ref with a leading ``/`` is taken alone, the slash stripped."""
    if ref.startswith("/"):
        return normalize_path(ref)
    return normalize_path(doc_path[: doc_path.rfind("/") + 1] + ref)


def fold_ref(top_path: str | None, chain: Sequence[str], ref: str) -> str:
    """The runtime's ``resolveRef``: ``ref`` folded through ``chain`` from ``top_path``.

    ``chain`` holds the embed refs from the top document down to the document
    that declares ``ref``. With ``top_path`` the result is in the same space as
    it (the runtime's ``screenPath``-given mode); with ``None`` it is relative to
    the top document's directory (the viewer's mode, where the loader joins it
    there).
    """
    doc = normalize_path(top_path or "")
    for link in chain:
        doc = join_ref(doc, link)
    return join_ref(doc, ref)


@dataclass(frozen=True, slots=True)
class Resolved:
    """Where one ref lands.

    ``path`` is its store path when it lands inside this root and is well formed
    (for a screen: names a valid draft id). ``reason`` says why it is unresolved,
    ``missing`` included when existence was asked. ``folded`` is the ref folded
    through its chain, relative to the top document's directory, with a leading
    ``/`` kept when the fold started over at one.
    """

    ref: str
    kind: RefKind
    folded: str
    path: str | None = None
    reason: Reason | None = None
    cross_root: bool = False
    root_relative: bool = False
    external: bool = False
    served: str | None = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        """The ref has a store path and nothing is wrong with where it points."""
        return self.path is not None and self.reason is None

    @property
    def draft_id(self) -> str | None:
        """The draft id a resolved screen ref names."""
        return screen_id(self.path) if self.ok and self.kind == "screen" else None

    @property
    def relpath(self) -> str | None:
        """The path below ``screens/`` (draft-id space) when the ref has a store path."""
        return self.path[len(ROOT_DIR) :] if self.path is not None else None


def resolve(
    top_path: str,
    chain: Sequence[str],
    ref: str,
    *,
    kind: RefKind = "screen",
    served_at: ServedAt = PERSONAL,
    tree: Tree | None = None,
    listing: Collection[str] | None = None,
) -> Resolved:
    """Resolve ``ref``, declared at the end of ``chain`` below the top document ``top_path``.

    ``top_path`` is the top document's store path (``screens/`` alone for a
    document with no id). Existence is asked only when a ``tree`` or a
    ``listing`` is given, and of the ``listing`` alone when both are.
    """
    if kind == "image":
        if _EXTERNAL_IMAGE.match(ref) or ref.startswith("/"):
            return Resolved(ref, kind, ref, external=True)
        if _SCHEME.match(ref):
            return Resolved(ref, kind, ref, reason="invalid_ref")
    elif is_absolute(ref):
        return Resolved(ref, kind, ref, reason="absolute", root_relative=ref.startswith("/"))

    relative = fold_ref(None, chain, ref)
    rooted = ref.startswith("/") or any(link.startswith("/") for link in chain)
    folded = f"/{relative}" if rooted else relative
    root_relative = kind == "screen" and folded.startswith(ROOT_RELATIVE_PREFIXES)

    # Where the URL lands: a library root is served one level down, under ``library/``.
    own = f"{LIBRARY}/{ROOT_DIR}" if served_at == LIBRARY else ROOT_DIR
    prefix = own[: -len(ROOT_DIR)]
    served = normalize_path(prefix + top_path[: top_path.rfind("/") + 1] + relative)

    def out(**found: Any) -> Resolved:
        return Resolved(ref, kind, folded, root_relative=root_relative, served=served, **found)

    if served == ".." or served.startswith("../"):
        return out(reason="above_root")
    if f"{served}/".startswith(own):
        relpath = served[len(own) :]
    elif f"{served}/".startswith(f"{LIBRARY}/") == (served_at != LIBRARY):
        # A personal ref that lands under ``library/``, or a library ref that lands outside it.
        return out(cross_root=True)
    else:
        return out(reason="above_root")

    if kind == "image":
        if asset_relpath_reason(relpath) is not None:
            return out(reason="invalid_ref")
    elif not relpath.endswith(SCREEN_SUFFIX) or not ID_PATTERN.fullmatch(relpath[: -len(SCREEN_SUFFIX)]):
        return out(reason="invalid_id")
    path = ROOT_DIR + relpath
    if (tree is not None or listing is not None) and not exists(tree, path, listing):
        return out(path=path, reason="missing")
    return out(path=path)


# --- findings -------------------------------------------------------------------------


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


def check_refs(
    doc: Any,
    store: DraftStore | Tree,
    draft_id: str | None = None,
    *,
    served_at: ServedAt | None = None,
    listing: Collection[str] | None = None,
) -> list[Finding]:
    """The ref findings for ``doc`` checked at ``draft_id``.

    ``doc`` is the document being checked (it need not be the stored copy);
    ``draft_id`` names the directory its refs resolve in (the root when
    ``None``). ``store`` is a :class:`DraftStore` or any tree: refs exist, and
    embedded documents are read, through it. ``served_at`` defaults to the
    store's own root (``personal`` for a tree). With a ``listing``, a ref exists
    only as an exact member of it.
    """
    if not isinstance(doc, dict):
        return []
    if draft_id is not None:
        DraftStore.validate_id(draft_id)  # raises InvalidDraftId
    if isinstance(store, DraftStore):
        served_at = served_at or (LIBRARY if store.category == LIBRARY else PERSONAL)
        tree: Tree = StoreTree(store)
    else:
        tree = store
    walker = _Walker(tree, screen_path(draft_id), served_at or PERSONAL, listing)
    walker.walk(doc, None, ())
    return walker.findings


class _Walker:
    def __init__(self, tree: Tree, top_path: str, served_at: ServedAt, listing: Collection[str] | None):
        self.tree = tree
        self.top_path = top_path
        self.served_at = served_at
        self.listing = listing
        self.visited: set[str] = {top_path}
        self.findings: list[Finding] = []

    def resolve(self, chain: tuple[str, ...], ref: str, kind: RefKind) -> Resolved:
        return resolve(
            self.top_path, chain, ref, kind=kind, served_at=self.served_at, tree=self.tree, listing=self.listing
        )

    def add(self, nid: str | None, code: str, path: Path, severity: Any, message: str, doc_id: str | None, **more: Any):
        self.findings.append(RefFinding(nid, code, pointer(path), severity, message, doc_id=doc_id, **more))

    def walk(self, doc: dict[str, Any], doc_id: str | None, chain: tuple[str, ...]) -> None:
        """Report ``doc``'s refs, then follow its embedded refs (``doc_id`` is ``None`` for the top document)."""
        here = self.top_path if doc_id is None else screen_path(doc_id)
        base_dir = here[len(ROOT_DIR) : max(here.rfind("/"), len(ROOT_DIR))]
        follow: list[tuple[str, str]] = []
        for nid, path, ref, embedded in iter_refs(doc):
            found = self.resolve(chain, ref, "screen")
            if found.root_relative:
                self.add(
                    nid, "root_relative_ref", path, "warning", _root_relative_message(found), doc_id, kind="screen"
                )
            if found.cross_root:
                self.add(nid, "cross_root_ref", path, "error", _cross_root_message(found), doc_id, kind="screen")
            elif found.reason is not None:
                message = _unresolved_message(ref, found.reason, base_dir)
                self.add(nid, "unresolved_ref", path, "warning", message, doc_id, reason=found.reason, kind="screen")
            elif embedded and found.path is not None:
                follow.append((found.path, ref))

        for nid, path, ref in iter_image_refs(doc):
            found = self.resolve(chain, ref, "image")
            if found.cross_root:
                self.add(nid, "cross_root_ref", path, "error", _cross_root_message(found), doc_id, kind="image")
            elif found.reason is not None:
                message = _image_message(ref, found.reason, base_dir, found.relpath)
                self.add(nid, "unresolved_ref", path, "warning", message, doc_id, reason=found.reason, kind="image")

        for child_path, ref in follow:
            if child_path in self.visited:
                continue
            self.visited.add(child_path)
            try:
                child = json.loads(self.tree.read(child_path))
            except (OSError, ValueError):
                continue  # unreadable embedded screens are reported by check(child_id)
            if isinstance(child, dict):
                self.walk(child, screen_id(child_path), (*chain, ref))


def _unresolved_message(ref: str, reason: Reason, base_dir: str) -> str:
    where = f"'{base_dir}/'" if base_dir else "the drafts root"
    return {
        "missing": f"ref {ref!r} names no stored draft (resolved against {where})",
        "absolute": f"ref {ref!r} is absolute; screen refs must be relative to the directory of the screen "
        "that declares them",
        "above_root": f"ref {ref!r} climbs above the drafts root (resolved against {where})",
        "invalid_id": f"ref {ref!r} does not name a valid '<draft id>{SCREEN_SUFFIX}'",
    }[reason]


def _cross_root_message(found: Resolved) -> str:
    what = "image" if found.kind == "image" else "ref"
    return (
        f"{what} {found.ref!r} lands in the other root (at {found.served!r}); a screen cannot use a file of "
        "another root in place: copy the part into your store, beside this screen, and reference the copy"
    )


def _root_relative_message(found: Resolved) -> str:
    return (
        f"ref {found.ref!r} is root-relative (it folds to {found.folded!r}); the viewer matches a ref that starts "
        f"with '/', '{ROOT_DIR}' or '{LIBRARY}/' against the whole store before the declaring screen's directory, "
        "so it stops naming the same file once the screen moves, and it cannot be pushed: write it relative to "
        "the declaring screen (or rename the directory it starts with)"
    )


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
        if node.get("type") == EMBED_TYPE and isinstance(file, str):
            out.append((nid, (*path, "props", "file"), file, True))
        actions = props.get("actions")
        for a, action in enumerate(actions if isinstance(actions, list) else []):
            if isinstance(action, dict) and action.get("type") == OPEN_DISPLAY_ACTION:
                target = action.get("file")
                if isinstance(target, str):
                    out.append((nid, (*path, "props", "actions", a, "file"), target, False))
    return out
