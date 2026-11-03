"""Text rules shared by every reader and writer: forbidden characters, case collisions, percent-decoding.

A forbidden character is one of Unicode general category ``Cc`` (C0, DEL, C1),
``Cf`` (format characters, the bidi controls among them) or ``Cs`` (a lone
surrogate). In free text (a pushed screen, a description) tab, LF and CR are
allowed; in a name (a file name, a saved-view or collection name, a tab label),
which must fit on one line, they are forbidden too.

Stdlib only.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import unquote
from collections.abc import Iterable

__all__ = [
    "FORBIDDEN_CATEGORIES",
    "MAX_UNQUOTE_ROUNDS",
    "TEXT_CONTROLS",
    "casefold_collisions",
    "fully_unquoted",
    "has_forbidden_char",
    "is_forbidden_char",
    "strip_forbidden",
]

FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
#: The controls free text may hold.
TEXT_CONTROLS = frozenset("\t\n\r")
#: Percent-decoding rounds before a path that keeps changing is refused.
MAX_UNQUOTE_ROUNDS = 8


def is_forbidden_char(char: str, *, in_name: bool = False) -> bool:
    """Whether the single character ``char`` is forbidden (in free text, or with ``in_name`` in a name)."""
    if not in_name and char in TEXT_CONTROLS:
        return False
    return unicodedata.category(char) in FORBIDDEN_CATEGORIES


def has_forbidden_char(text: str, *, in_name: bool = False) -> bool:
    """Whether any character of ``text`` is forbidden (:func:`is_forbidden_char`)."""
    return any(is_forbidden_char(char, in_name=in_name) for char in text)


def strip_forbidden(text: str, *, in_name: bool = True) -> str:
    """``text`` without its forbidden characters; by default the name rule, so tab, LF and CR go too."""
    return "".join(char for char in text if not is_forbidden_char(char, in_name=in_name))


def casefold_collisions(paths: Iterable[str], *, directories: Iterable[str] = ()) -> list[list[str]]:
    """The names among ``paths`` and their directory prefixes that are not unique under ``str.casefold()``.

    ``paths`` are ``/``-separated relative paths, read as a set; ``directories``
    are further names known to be directories (an empty one has no member to
    imply it). Every member, every listed directory and every distinct directory
    prefix of a member is one name; names that share a casefold collide. Each
    collision is the sorted list of its spellings, and the collisions are sorted,
    so the answer does not depend on input order. A collision with a single
    spelling is a member that is also a directory. Empty when a case-insensitive
    filesystem can hold the set as written.
    """
    members = set(paths)
    prefixes = {path[:at] for path in members for at, char in enumerate(path) if char == "/"}
    dirs = prefixes | set(directories)
    groups: dict[str, list[str]] = {}
    for name in (*members, *dirs):
        groups.setdefault(name.casefold(), []).append(name)
    return sorted(sorted(set(names)) for names in groups.values() if len(names) > 1)


def fully_unquoted(path: str) -> str | None:
    """``path`` percent-decoded until it stops changing, so ``%252e`` cannot hide a ``..``.

    ``None`` when it still changes after :data:`MAX_UNQUOTE_ROUNDS` rounds: a path
    nested that deep is refused rather than checked half decoded.
    """
    for _ in range(MAX_UNQUOTE_ROUNDS):
        decoded = unquote(path)
        if decoded == path:
            return path
        path = decoded
    return None
