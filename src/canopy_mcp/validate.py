"""Value rules shared by the saved views, the collections and the workspace hub.

Ids, names and tool queries mean the same thing wherever the viewer sends them,
so each rule is written once here.
"""

from __future__ import annotations

import re
import json
from typing import Any

from canopy_mcp.text import has_forbidden_char

__all__ = [
    "MAX_NAME_CHARS",
    "MAX_QUERY_BYTES",
    "TOOL_IDS",
    "VIEW_ID_PATTERN",
    "InvalidValue",
    "check_query",
    "clean_name",
    "compact_size",
    "query_problem",
]

#: A saved-view or collection id.
VIEW_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_NAME_CHARS = 120
#: The longest tool query, in UTF-8 bytes.
MAX_QUERY_BYTES = 4096
#: Every tool the viewer can open. A stored member or tab may name any of them,
#: whether or not this host serves the tool's page.
TOOL_IDS: tuple[str, ...] = ("trace", "correlation", "waveform")


class InvalidValue(ValueError):
    """A value breaks one of the rules here; the message names the rule."""


def clean_name(name: Any) -> str:
    """``name`` stripped, when it is a 1..120-character string without a forbidden character.

    Forbidden is the name rule of :mod:`canopy_mcp.text`: control and format
    characters and lone surrogates (which cannot be written as UTF-8).
    """
    if not isinstance(name, str):
        raise InvalidValue("name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise InvalidValue(f"name must be 1 to {MAX_NAME_CHARS} characters after stripping")
    if has_forbidden_char(name, in_name=True):
        raise InvalidValue("name must not contain control characters, format characters or lone surrogates")
    return name


def query_problem(query: Any) -> str | None:
    """Why ``query`` is not a tool query (a string of at most :data:`MAX_QUERY_BYTES` UTF-8 bytes), or ``None``."""
    if not isinstance(query, str):
        return "query must be a string"
    try:
        size = len(query.encode("utf-8"))
    except UnicodeEncodeError:
        return "query must be valid UTF-8"
    if size > MAX_QUERY_BYTES:
        return f"query must be at most {MAX_QUERY_BYTES} UTF-8 bytes"
    return None


def check_query(query: Any) -> str:
    """``query`` when it is a tool query (empty allowed); :class:`InvalidValue` naming the rule otherwise."""
    problem = query_problem(query)
    if problem is not None:
        raise InvalidValue(problem)
    return query


def compact_size(value: Any) -> int:
    """The size of ``value`` as compact UTF-8 JSON, the measure every document limit uses.

    A lone surrogate counts as its three bytes rather than raising, so an
    oversized document is reported as oversized whatever it holds.
    """
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))
