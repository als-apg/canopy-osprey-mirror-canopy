"""The identity of a collection member: ``member_key(member)``.

A member is one of two persisted shapes::

    {"screen": "<category>/<id>", "macros"?: {"<name>": "<value>", ...}}
    {"tool": "<tool id>", "query"?: "<opaque>", "name"?: "<label>"}

Its key is a string of its own grammar, never a viewer tab key:

- ``screen:<category>/<id>`` followed, when at least one macro survives, by ``?`` and the
  canonical macros;
- ``tool:<tool>`` followed, when ``query`` is present, by ``?`` and the query exactly as
  stored. An absent query (the tool's config default) and a blank one (``""``) differ:
  ``tool:trace`` against ``tool:trace?``.

The canonical macros: the names the viewer keeps for itself are dropped (exactly
``embedded``, ``theme`` and ``mode``, and every name starting ``_canopy``; case-sensitive),
the remaining names are sorted by Unicode code point (not by UTF-16 code unit), and each
``name=value`` pair is written with both sides percent-encoded per RFC 3986 over UTF-8:
the unreserved characters ``A-Z a-z 0-9 - . _ ~`` stay literal, every other byte becomes
``%XX`` in upper-case hex (a space is ``%20``, never ``+``). Pairs are joined by ``&``.

The screen reference, the tool id and the query are written verbatim; their own grammar
is the validator's concern. ``name`` never contributes to the key.

``fixtures/collections/member-key-vectors.json`` under ``tests/canopy_mcp`` pins this
grammar for every implementation; a member this function cannot key raises
:class:`InvalidMember`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from collections.abc import Mapping

from canopy_mcp.report import RESERVED_URL_PARAMS, RESERVED_URL_PREFIX

SCREEN_PREFIX = "screen:"
TOOL_PREFIX = "tool:"


class InvalidMember(ValueError):
    """The value is not a member that has a key."""


def is_reserved_macro(name: str) -> bool:
    """True when ``name`` is a query name the viewer keeps for itself."""
    return name in RESERVED_URL_PARAMS or name.startswith(RESERVED_URL_PREFIX)


def _encode(text: str) -> str:
    try:
        return quote(text, safe="", encoding="utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidMember("a macro is not valid Unicode text") from exc


def canonical_macros(macros: Mapping[str, Any]) -> str:
    """The canonical query of ``macros``; ``""`` when no macro survives."""
    pairs = []
    for name in sorted(macros):
        value = macros[name]
        if not isinstance(name, str) or not isinstance(value, str):
            raise InvalidMember("a macro name and value must be strings")
        if not is_reserved_macro(name):
            pairs.append(f"{_encode(name)}={_encode(value)}")
    return "&".join(pairs)


def member_key(member: Mapping[str, Any]) -> str:
    """The identity key of ``member``; raises :class:`InvalidMember` for anything else."""
    if not isinstance(member, Mapping):
        raise InvalidMember("a member must be an object")
    has_screen, has_tool = "screen" in member, "tool" in member
    if has_screen == has_tool:
        raise InvalidMember("a member names exactly one of screen and tool")
    if has_screen:
        screen = member["screen"]
        if not isinstance(screen, str) or not screen:
            raise InvalidMember("screen must be a non-empty string")
        macros = member.get("macros", {})
        if not isinstance(macros, Mapping):
            raise InvalidMember("macros must be an object")
        if any(not isinstance(name, str) for name in macros):
            raise InvalidMember("a macro name and value must be strings")
        query = canonical_macros(macros)
        return f"{SCREEN_PREFIX}{screen}?{query}" if query else f"{SCREEN_PREFIX}{screen}"
    tool = member["tool"]
    if not isinstance(tool, str) or not tool:
        raise InvalidMember("tool must be a non-empty string")
    if "query" not in member:
        return f"{TOOL_PREFIX}{tool}"
    query = member["query"]
    if not isinstance(query, str):
        raise InvalidMember("query must be a string")
    return f"{TOOL_PREFIX}{tool}?{query}"
