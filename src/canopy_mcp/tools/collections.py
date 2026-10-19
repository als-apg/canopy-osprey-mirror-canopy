"""``list_collections``: every collection the viewer offers, as the agent sees it.

Read-only. The collections are the ones ``GET /collections`` lists, in the same
order (facility first, then personal, each by case-folded name, then id), read
from the same directories (``facility_collections_dir`` and
``personal_collections_dir``). Each member is checked against this host:

- a screen member (``library/<id>`` or ``drafts/<id>``) is available when the
  merged library+drafts manifest lists it, and carries ``title`` (the manifest
  title, else the member's screen reference), ``url``, the embed URL with the
  member's macros as query parameters, and ``ref`` (plus ``macros`` when it has
  any), the arguments the workspace ``open`` command takes;
- a tool member is available when this host serves the tool
  (:func:`canopy_mcp.static.configured_tools`), and carries ``label`` (its
  ``name``, else the tool's label), ``open_url: null``, and ``tool`` (plus
  ``query`` when it has one), the arguments the workspace ``open_tool`` command
  takes.

A collection's ``open_url`` is the viewer's embed root with ``_canopyOpen=<ref>``:
opening it opens every member of the collection once.
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
from urllib.parse import quote, urlencode

from canopy_mcp.urls import embed_path
from canopy_mcp.store import DRAFTS
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.static import configured_tools
from canopy_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT
from canopy_mcp.member_key import InvalidMember, member_key
from canopy_mcp.collections import CollectionStore, FacilityCollections, all_collections
from canopy_mcp.tools.drafts import screen_ref
from canopy_mcp.tools._common import get_store, get_library_store

# What the viewer calls each tool; a tool member without a ``name`` is labelled by it.
TOOL_LABELS = {"trace": "Trace", "correlation": "Correlation", "waveform": "Waveform"}

# One pair of readers per (facility dir, personal dir), so a bad file is logged once.
_readers: dict[tuple[Path | None, Path], tuple[FacilityCollections | None, CollectionStore]] = {}


def reset_readers() -> None:
    """Forget every cached reader (tests switch directories)."""
    _readers.clear()


def _open_readers(settings: Settings) -> tuple[FacilityCollections | None, CollectionStore]:
    facility_dir = settings.facility_collections_dir
    personal_dir = Path(settings.personal_collections_dir)
    key = (Path(facility_dir) if facility_dir is not None else None, personal_dir)
    pair = _readers.get(key)
    if pair is None:
        facility = FacilityCollections(facility_dir) if facility_dir is not None else None
        pair = _readers[key] = (facility, CollectionStore(personal_dir, facility=facility))
    return pair


def _absolute(settings: Settings, path: str) -> str:
    """``path`` on the viewer: panel-relative inside OSPREY, else on this server's address."""
    if settings.panel_id:
        return f"/panel/{settings.panel_id}{path}"
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}{path}"


def _screens(settings: Settings) -> dict[str, str]:
    """``<category>/<id>`` -> title for every IR screen of the merged library+drafts manifest."""
    stores = [get_store(settings), get_library_store(settings)]
    screens: dict[str, str] = {}
    for store in stores:
        if store is None:
            continue
        for entry in store.manifest()["screens"]:
            if entry.get("kind") == "ir":
                screens[f"{entry['category']}/{entry['id']}"] = entry["title"]
    return screens


def _key(member: dict[str, Any]) -> str:
    try:
        return member_key(member)
    except InvalidMember:  # a listed collection is valid, so this cannot happen
        return ""


def _screen_member(settings: Settings, member: dict[str, Any], screens: dict[str, str]) -> dict[str, Any]:
    ref = member["screen"]
    category, _, local_id = ref.partition("/")
    url = _absolute(settings, embed_path(quote(local_id, safe="/"), category or DRAFTS))
    macros = member.get("macros") or {}
    if macros:
        url += "?" + urlencode(macros, quote_via=quote)
    entry = {
        "key": _key(member),
        "available": ref in screens,
        "title": screens.get(ref, ref),
        "url": url,
        "ref": screen_ref(category or DRAFTS, local_id),
    }
    if macros:
        entry["macros"] = dict(macros)
    return entry


def _tool_member(member: dict[str, Any], tools: tuple[str, ...]) -> dict[str, Any]:
    tool = member["tool"]
    entry = {
        "key": _key(member),
        "available": tool in tools,
        "label": member.get("name") or TOOL_LABELS.get(tool, tool),
        "open_url": None,
        "tool": tool,
    }
    if "query" in member:
        entry["query"] = member["query"]
    return entry


@mcp.tool
async def list_collections() -> dict[str, Any]:
    """List every collection: named sets of screens and tools the viewer opens together.

    Returns ``{collections: [...]}``, facility collections first, then the user's
    own. Each has ``ref`` (``<source>:<id>``), ``source`` (``facility`` or
    ``personal``), ``name``, ``description``, ``default`` (shown to every user who
    has not unticked it), ``available_count`` and ``open_url`` (the viewer opens
    every member of the collection at once). Each member has ``key`` and
    ``available`` (whether this host can open it); a screen member adds ``title``,
    ``url`` (opens that one screen) and ``ref`` with ``macros`` when it has any
    (the workspace ``open`` arguments), a tool member (Trace, Correlation,
    Waveform) adds ``label``, ``open_url: null`` and ``tool`` with ``query`` when
    it has one (the workspace ``open_tool`` arguments). A collection's ``ref`` is
    what the workspace ``open_collection`` command takes.

    Answer with collection names and member counts, render ``open_url`` as a link,
    and never quote field names to the operator.
    """
    settings = get_settings()
    facility, personal = _open_readers(settings)
    screens = _screens(settings)
    tools = configured_tools(settings)
    collections = []
    for doc in all_collections(facility, personal):
        members = [
            _screen_member(settings, member, screens) if "screen" in member else _tool_member(member, tools)
            for member in doc["members"]
        ]
        collections.append(
            {
                "ref": doc["ref"],
                "source": doc["source"],
                "name": doc["name"],
                "description": doc.get("description", ""),
                "default": doc.get("default", False) is True,
                "open_url": _absolute(settings, "/embed/?" + urlencode({"_canopyOpen": doc["ref"]})),
                "available_count": sum(1 for member in members if member["available"]),
                "members": members,
            }
        )
    return {"collections": collections}
