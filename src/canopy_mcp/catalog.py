"""The widget catalog: every Screen-IR widget type an agent may place.

Two sources describe a widget type, and neither is complete on its own:

* **Beaver's registry** (``GET /beaver/v1/widgets`` and one
  ``GET /beaver/v1/widgets/{id}`` per id) carries a draft-07 ``propSchema``
  per widget. The list endpoint returns summaries only, so the details are
  fetched concurrently, once, when the catalog loads.
* **``ir-map.json``**, emitted into the viewer build (``CANOPY_VIEWER_DIST``),
  names every IR type the renderer maps and the props each mapping
  ``consumes``. Types the renderer copies through ``verbatim`` consume nothing
  by name.

The catalog has one entry per ``ir-map.json`` type. Its props are the union of
the Beaver ``propSchema`` properties and the renderer's ``consumes`` list, and
each prop records which source(s) named it. A ``verbatim`` type Beaver does
not document is marked ``undocumented``: the renderer draws it, but nothing
says which props it reads. ``unknown-widget`` is the one extra type, the
placeholder a converter emits for a legacy widget it could not map.
"""

import copy
import json
import asyncio
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass

import httpx

from canopy_mcp.check import schema as ir_schema
from canopy_mcp.client import borrowed_client
from canopy_mcp.config import Settings, get_settings

WIDGETS_PATH = "/beaver/v1/widgets"
IR_MAP_FILENAME = "ir-map.json"
PLACEHOLDER_TYPE = "unknown-widget"

SOURCE_BEAVER = "beaver"
SOURCE_CONSUMES = "consumes"


class CatalogError(RuntimeError):
    """The catalog cannot be built; the server must not start without it."""


@dataclass(frozen=True)
class PropInfo:
    """One prop of a widget type and the sources that name it."""

    name: str
    sources: tuple[str, ...]
    schema: dict[str, Any] | None = None
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources),
            "schema": self.schema,
            "required": self.required,
        }


@dataclass(frozen=True)
class CatalogEntry:
    """Everything known about one IR widget type."""

    type: str
    props: dict[str, PropInfo]
    # Beaver's full draft-07 propSchema; None when Beaver has no entry.
    prop_schema: dict[str, Any] | None = None
    consumes: tuple[str, ...] = ()
    verbatim: bool = False
    undocumented: bool = False
    placeholder: bool = False
    display_name: str | None = None
    description: str | None = None
    category: str | None = None
    supports_children: bool | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "displayName": self.display_name,
            "category": self.category,
            "description": self.description,
            "verbatim": self.verbatim,
            "undocumented": self.undocumented,
            "placeholder": self.placeholder,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "supportsChildren": self.supports_children,
            "consumes": list(self.consumes),
            "props": {name: info.to_dict() for name, info in self.props.items()},
            "propSchema": self.prop_schema,
        }


@dataclass(frozen=True)
class Catalog:
    """The joined catalog, keyed by IR type."""

    entries: dict[str, CatalogEntry]
    # Beaver ids with no ir-map.json type: the renderer cannot draw them.
    unmapped_beaver_ids: tuple[str, ...] = field(default=())

    def get(self, widget_type: str) -> CatalogEntry | None:
        return self.entries.get(widget_type)

    def __contains__(self, widget_type: object) -> bool:
        return widget_type in self.entries

    @property
    def types(self) -> list[str]:
        return sorted(self.entries)


# --- Sources ----------------------------------------------------------------


def load_ir_map(viewer_dist: Path | None) -> dict[str, dict[str, Any]]:
    """Read ``ir-map.json`` from the viewer build directory."""
    if viewer_dist is None:
        raise CatalogError("CANOPY_VIEWER_DIST is not set; cannot read ir-map.json")
    path = Path(viewer_dist) / IR_MAP_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"{path} not found; is CANOPY_VIEWER_DIST a built viewer?") from exc
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise CatalogError(f"{path} holds no widget types")
    for widget_type, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("consumes", []), list):
            raise CatalogError(f"{path}: malformed entry for {widget_type!r}")
    return data


async def fetch_registry(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch every Beaver widget with its ``propSchema``.

    The list endpoint returns summaries without ``propSchema``, so each id's
    details are fetched concurrently. Refuses an empty registry.
    """
    try:
        response = await client.get(WIDGETS_PATH)
        response.raise_for_status()
        listing = response.json()
    except httpx.HTTPError as exc:
        raise CatalogError(f"GET {WIDGETS_PATH} failed: {exc}") from exc

    if not listing.get("total") or not listing.get("widgets"):
        raise CatalogError(f"GET {WIDGETS_PATH} returned no widgets (total == 0)")

    ids = [summary["id"] for summary in listing["widgets"]]

    async def detail(widget_id: str) -> dict[str, Any]:
        try:
            resp = await client.get(f"{WIDGETS_PATH}/{widget_id}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise CatalogError(f"GET {WIDGETS_PATH}/{widget_id} failed: {exc}") from exc

    return list(await asyncio.gather(*(detail(widget_id) for widget_id in ids)))


# --- Join -------------------------------------------------------------------


def join_coverage(beaver_ids: list[str], ir_map_types: list[str]) -> list[str]:
    """Return the Beaver ids that ``ir-map.json`` does not know (sorted)."""
    known = set(ir_map_types)
    return sorted(set(beaver_ids) - known)


def _props_for(beaver: dict[str, Any] | None, consumes: list[str]) -> dict[str, PropInfo]:
    schema = (beaver or {}).get("propSchema") or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    consumed = set(consumes)

    props: dict[str, PropInfo] = {}
    for name in [*properties, *(c for c in consumes if c not in properties)]:
        sources = tuple(
            source
            for source, present in ((SOURCE_BEAVER, name in properties), (SOURCE_CONSUMES, name in consumed))
            if present
        )
        props[name] = PropInfo(
            name=name,
            sources=sources,
            schema=properties.get(name),
            required=name in required,
        )
    return props


def _placeholder_entry() -> CatalogEntry:
    return CatalogEntry(
        type=PLACEHOLDER_TYPE,
        props={},
        placeholder=True,
        display_name="Unknown widget",
        category="placeholder",
        description=(
            "Placeholder for a legacy widget the converter could not map; "
            "props.originalClass names the source class. Replace it with a real type."
        ),
        supports_children=False,
    )


def build_catalog(details: list[dict[str, Any]], ir_map: dict[str, dict[str, Any]]) -> Catalog:
    """Join Beaver widget details with ``ir-map.json`` into one catalog."""
    by_id = {detail["id"]: detail for detail in details}
    entries: dict[str, CatalogEntry] = {}
    for widget_type, mapping in ir_map.items():
        beaver = by_id.get(widget_type)
        consumes = list(mapping.get("consumes") or [])
        verbatim = bool(mapping.get("verbatim", False))
        entries[widget_type] = CatalogEntry(
            type=widget_type,
            props=_props_for(beaver, consumes),
            prop_schema=beaver.get("propSchema") if beaver else None,
            consumes=tuple(consumes),
            verbatim=verbatim,
            undocumented=verbatim and beaver is None,
            display_name=beaver.get("displayName") if beaver else None,
            description=beaver.get("description") if beaver else None,
            category=beaver.get("category") if beaver else None,
            supports_children=beaver.get("supportsChildren") if beaver else None,
        )
    entries[PLACEHOLDER_TYPE] = _placeholder_entry()
    return Catalog(
        entries=entries,
        unmapped_beaver_ids=tuple(join_coverage(list(by_id), list(ir_map))),
    )


async def load_catalog(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> Catalog:
    """Build the catalog from the live registry and the viewer's ``ir-map.json``."""
    settings = settings or get_settings()
    ir_map = load_ir_map(settings.viewer_dist)
    async with borrowed_client(client, settings) as http:
        details = await fetch_registry(http)
    return build_catalog(details, ir_map)


# --- Process-wide catalog ---------------------------------------------------

_catalog: Catalog | None = None
_lock = asyncio.Lock()


def set_catalog(catalog: Catalog | None) -> None:
    """Install (or, with ``None``, clear) the process-wide catalog."""
    global _catalog
    _catalog = catalog


async def ensure_catalog(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> Catalog:
    """Return the process-wide catalog, loading it on first use."""
    global _catalog
    if _catalog is not None:
        return _catalog
    async with _lock:
        if _catalog is None:
            _catalog = await load_catalog(settings, client)
    return _catalog


# --- Screen-IR schema -------------------------------------------------------


def load_schema() -> dict[str, Any]:
    """Return the vendored Screen-IR JSON Schema (a fresh copy per call).

    The check pass owns the (cached) load; the copy keeps a caller's edits out of it.
    """
    return copy.deepcopy(ir_schema.load_schema())
