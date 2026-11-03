"""The widget catalog: every Screen-IR widget type an agent may place.

The viewer build's ``ir-map.json`` (``CANOPY_VIEWER_DIST``) is the catalog: it
names every IR type the renderer maps, the props each mapping ``consumes``,
whether the type is copied through ``verbatim``, and the editor metadata
(``displayName``, ``description``, ``category``, ``supportsChildren``) the
runtime carries from ``@slaclab/canopy-screen-widgets``.

Prop schemas come from the spec staging directory
(``Settings.staged_spec_dir``), one draft-07 ``widgets/<ir id>.props.schema.json``
per documented type. A type's props are the union of its prop-schema
properties and its ``consumes`` list, each prop recording which source(s)
named it. A ``verbatim`` type without a prop schema is marked
``undocumented``: the renderer draws it, but nothing says which props it reads.
``unknown-widget`` is the one extra type, the placeholder a converter emits for
a legacy widget it could not map.
"""

import json
import asyncio
from typing import Any
from pathlib import Path
from dataclasses import dataclass

from canopy_mcp.config import Settings, get_settings

IR_MAP_FILENAME = "ir-map.json"
PLACEHOLDER_TYPE = "unknown-widget"
PROPS_SCHEMA_DIR = "widgets"
PROPS_SCHEMA_SUFFIX = ".props.schema.json"

SOURCE_CATALOG = "catalog"
SOURCE_CONSUMES = "consumes"


class CatalogError(RuntimeError):
    """Raised when the catalog cannot be built; the server starts without it and retries on first use."""


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
    # The staged draft-07 prop schema; None when the spec has none for the type.
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
    """The catalog, keyed by IR type."""

    entries: dict[str, CatalogEntry]

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


def load_prop_schemas(spec_dir: Path | None) -> dict[str, dict[str, Any]]:
    """The staged per-widget prop schemas, keyed by IR id; empty without a staging directory.

    A staged file that is not a JSON object is a :class:`CatalogError`: a
    broken spec must not pass as an undocumented one.
    """
    directory = Path(spec_dir) / PROPS_SCHEMA_DIR if spec_dir is not None else None
    if directory is None or not directory.is_dir():
        return {}
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob(f"*{PROPS_SCHEMA_SUFFIX}")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"cannot read {path}: {exc}") from exc
        if not isinstance(schema, dict):
            raise CatalogError(f"{path} is not a JSON object")
        schemas[path.name.removesuffix(PROPS_SCHEMA_SUFFIX)] = schema
    return schemas


# --- Build ------------------------------------------------------------------


def _props_for(schema: dict[str, Any] | None, consumes: list[str]) -> dict[str, PropInfo]:
    schema = schema or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    consumed = set(consumes)

    props: dict[str, PropInfo] = {}
    for name in [*properties, *(c for c in consumes if c not in properties)]:
        sources = tuple(
            source
            for source, present in ((SOURCE_CATALOG, name in properties), (SOURCE_CONSUMES, name in consumed))
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


def build_catalog(ir_map: dict[str, dict[str, Any]], prop_schemas: dict[str, dict[str, Any]] | None = None) -> Catalog:
    """Build the catalog from ``ir-map.json`` and the staged prop schemas (by IR id)."""
    prop_schemas = prop_schemas or {}
    entries: dict[str, CatalogEntry] = {}
    for widget_type, mapping in ir_map.items():
        schema = prop_schemas.get(widget_type)
        consumes = list(mapping.get("consumes") or [])
        verbatim = bool(mapping.get("verbatim", False))
        entries[widget_type] = CatalogEntry(
            type=widget_type,
            props=_props_for(schema, consumes),
            prop_schema=schema,
            consumes=tuple(consumes),
            verbatim=verbatim,
            undocumented=verbatim and schema is None,
            display_name=mapping.get("displayName"),
            description=mapping.get("description"),
            category=mapping.get("category"),
            supports_children=mapping.get("supportsChildren"),
        )
    entries[PLACEHOLDER_TYPE] = _placeholder_entry()
    return Catalog(entries=entries)


def _build_from(settings: Settings) -> Catalog:
    return build_catalog(load_ir_map(settings.viewer_dist), load_prop_schemas(settings.staged_spec_dir))


async def load_catalog(settings: Settings | None = None) -> Catalog:
    """Build the catalog from the viewer's ``ir-map.json`` and the staged prop schemas.

    The files are read in a worker thread, so the event loop is not held up.
    """
    return await asyncio.to_thread(_build_from, settings or get_settings())


# --- Process-wide catalog ---------------------------------------------------

_catalog: Catalog | None = None
_lock = asyncio.Lock()


def set_catalog(catalog: Catalog | None) -> None:
    """Install (or, with ``None``, clear) the process-wide catalog."""
    global _catalog
    _catalog = catalog


async def ensure_catalog(settings: Settings | None = None) -> Catalog:
    """Return the process-wide catalog, loading it on first use."""
    global _catalog
    if _catalog is not None:
        return _catalog
    async with _lock:
        if _catalog is None:
            _catalog = await load_catalog(settings)
    return _catalog
