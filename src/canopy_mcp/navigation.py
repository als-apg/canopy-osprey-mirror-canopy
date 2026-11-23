"""A library's own navigation: the optional ``navigation.json`` at the library root.

The file orders the library's screens for the viewer's sidebar. It is a JSON list
of entries, nested into named groups::

    [
      {"screen": "library/overview", "label": "Start here"},
      {"label": "Pumps", "screen": "library/pumps/all", "children": [
        {"screen": "library/pumps/p1"},
        {"label": "Spares", "children": [{"screen": "library/pumps/spare"}]}
      ]}
    ]

A leaf names a screen as the manifest does, ``<category>/<id>``, with an optional
``label``. A group has a ``label`` and ``children``, and may name a ``screen`` of
its own. No other key is allowed. The server checks the shape and the limits
below and hands a valid file to the viewer unchanged, as the manifest's
``navigation``; whether a named screen exists is the viewer's to decide, since it
holds the whole listing. A missing file means no navigation. An invalid one is
ignored with one logged warning for each state of the file.
"""

import json
import logging
from typing import Any
from pathlib import Path

from canopy_mcp.store import contained_file

NAVIGATION_FILE = "navigation.json"

MAX_NAVIGATION_BYTES = 256 * 2**10
MAX_NAVIGATION_DEPTH = 8
MAX_NAVIGATION_ENTRIES = 2000
MAX_NAVIGATION_LABEL = 120
MAX_NAVIGATION_SCREEN = 512

_KEYS = frozenset({"screen", "label", "children"})

logger = logging.getLogger(__name__)

# The stat key that stands for a path ``contained_file`` refuses, so it is warned about once.
_REFUSED = (-1, -1, -1)


class InvalidNavigation(ValueError):
    """``navigation.json`` does not have the shape or stay within the limits this module states."""


def check_navigation(entries: object) -> list[Any]:
    """``entries`` itself when it is a valid navigation; ``InvalidNavigation`` naming the first fault otherwise."""
    count = 0

    def walk(items: object, where: str, depth: int) -> None:
        nonlocal count
        if not isinstance(items, list):
            raise InvalidNavigation(f"{where}: expected a list of entries")
        if depth > MAX_NAVIGATION_DEPTH:
            raise InvalidNavigation(f"{where}: nested deeper than {MAX_NAVIGATION_DEPTH} levels")
        for index, entry in enumerate(items):
            here = f"{where}[{index}]"
            count += 1
            if count > MAX_NAVIGATION_ENTRIES:
                raise InvalidNavigation(f"more than {MAX_NAVIGATION_ENTRIES} entries")
            if not isinstance(entry, dict):
                raise InvalidNavigation(f"{here}: expected an object")
            unknown = sorted(set(entry) - _KEYS)
            if unknown:
                raise InvalidNavigation(f"{here}: unknown key {unknown[0]!r}")
            _check_text(entry, "screen", MAX_NAVIGATION_SCREEN, here)
            _check_text(entry, "label", MAX_NAVIGATION_LABEL, here)
            if "children" in entry:
                if "label" not in entry:
                    raise InvalidNavigation(f"{here}: a group needs a label")
                walk(entry["children"], f"{here}.children", depth + 1)
            elif "screen" not in entry:
                raise InvalidNavigation(f"{here}: needs a screen or children")

    walk(entries, "navigation", 1)
    return entries  # type: ignore[return-value]


def _check_text(entry: dict[str, Any], key: str, longest: int, here: str) -> None:
    if key not in entry:
        return
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise InvalidNavigation(f"{here}.{key}: expected non-empty text")
    if len(value) > longest:
        raise InvalidNavigation(f"{here}.{key}: longer than {longest} characters")


class NavigationFile:
    """``<root>/navigation.json``, read again only when its stat key changes.

    The viewer polls the manifest, so ``read`` follows the rule of the store's
    listing: an unchanged ``(st_ino, st_mtime_ns, st_size)`` answers from what was
    last read, and an invalid file is warned about once for that state, not once
    for each poll.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._known: tuple[tuple[int, int, int], list[Any] | None] | None = None

    def read(self) -> list[Any] | None:
        """The navigation, or ``None`` when the file is missing, outside the root or invalid."""
        target = contained_file(self.root, NAVIGATION_FILE)
        if target is None:
            named = self.root / NAVIGATION_FILE
            if not named.is_symlink() and not named.exists():
                self._known = None
            elif self._known != (_REFUSED, None):
                logger.warning("navigation: %s: not a regular file inside the library root (ignored)", named)
                self._known = (_REFUSED, None)
            return None
        try:
            st = target.stat()
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            if self._known is not None and self._known[0] == key:
                return self._known[1]
            navigation = self._load(target, st.st_size)
        except OSError as exc:
            logger.warning("navigation: %s: %s: %s (ignored)", target, type(exc).__name__, exc)
            self._known = None
            return None
        self._known = (key, navigation)
        return navigation

    def _load(self, target: Path, size: int) -> list[Any] | None:
        try:
            if size > MAX_NAVIGATION_BYTES:
                raise InvalidNavigation(f"{size} bytes exceeds the {MAX_NAVIGATION_BYTES}-byte limit")
            # One byte past the limit: a file that grew after the stat is still refused.
            with target.open("rb") as handle:
                data = handle.read(MAX_NAVIGATION_BYTES + 1)
            if len(data) > MAX_NAVIGATION_BYTES:
                raise InvalidNavigation(f"exceeds the {MAX_NAVIGATION_BYTES}-byte limit")
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise InvalidNavigation(f"not JSON: {exc}") from None
            return check_navigation(parsed)
        except InvalidNavigation as exc:
            logger.warning("navigation: %s: %s (ignored)", target, exc)
            return None
