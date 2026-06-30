"""Seam #1: a ``RegistryClient`` over the Beaver widget-registry module.

The lookups are *injected* (id -> payload, qt class -> payload) so the mapping
logic (raw payload -> ``WidgetDefinition``, ``None`` on a miss) stays pure and
tested. ``build_registry`` selects this or the vendored snapshot by config.

Beaver merged (PR #21). Its gateway is **async** and raises on a miss, while
this ``RegistryClient`` is **sync** and returns ``None``; ``from_canopy_gateway``
bridges both (see its docstring). The default still keeps the vendored snapshot
shipped with ``pydmconverter`` — the live path is opt-in via
``CANOPY_CONVERTER__USE_BEAVER_GATEWAY=true``.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from pydmconverter.ir.registry import RegistryClient, VendoredRegistry, WidgetDefinition

from canopy.converter.config import ConverterConfig

# A Beaver lookup returns the raw widget payload (a superset of WidgetDefinition;
# extra fields are ignored on validation) or ``None`` when the widget is unknown.
BeaverLookup = Callable[[str], dict[str, Any] | None]


def _to_payload(dto: Any) -> dict[str, Any]:
    """Dump a Beaver ``WidgetDTO`` to a coercible camelCase payload.

    ``None``-valued keys are dropped: Beaver marks absent optional fields (e.g.
    ``qtMapping``, ``qtPropMap``) as ``None``, but the corresponding
    ``WidgetDefinition`` fields are non-optional dicts with defaults, so an
    explicit ``None`` would fail validation. Dropping the key lets the default
    apply; ``extra="ignore"`` handles the richer Beaver-only fields.
    """
    return {key: value for key, value in dto.model_dump(by_alias=True).items() if value is not None}


class BeaverGatewayRegistry:
    """``RegistryClient`` backed by Beaver lookups (id -> def, qt class -> def)."""

    def __init__(self, *, by_id_lookup: BeaverLookup, by_qt_class_lookup: BeaverLookup) -> None:
        self._by_id_lookup = by_id_lookup
        self._by_qt_class_lookup = by_qt_class_lookup

    def by_id(self, widget_id: str) -> WidgetDefinition | None:
        return self._coerce(self._by_id_lookup(widget_id))

    def by_qt_class(self, qt_class: str) -> WidgetDefinition | None:
        return self._coerce(self._by_qt_class_lookup(qt_class))

    @staticmethod
    def _coerce(payload: dict[str, Any] | None) -> WidgetDefinition | None:
        # WidgetDefinition(extra="ignore") maps Beaver's richer payload straight in.
        return None if payload is None else WidgetDefinition.model_validate(payload)

    @classmethod
    def from_canopy_gateway(cls) -> BeaverGatewayRegistry:
        """Build a registry over the live ``canopy.beaver.gateway``.

        Bridges three gaps between Beaver's gateway and the ``RegistryClient``
        contract:

        - **async -> sync.** Conversion runs in a worker thread (the route does
          ``run_in_threadpool(service.convert)``), so ``by_id``/``by_qt_class``
          are called synchronously off the event loop, while Beaver's lookups are
          ``async``. ``anyio.from_thread.run`` re-enters the running loop from the
          worker thread.
        - **miss -> None.** Beaver raises ``WidgetNotFound`` (``get``) and
          ``QtClassNotMapped`` (``by_qt_class``) on a miss; the contract wants
          ``None`` so the builder emits an ``unknown-widget`` node.
        - **``WidgetDTO`` -> ``WidgetDefinition``.** Both are camelCase-aliased,
          ``extra="ignore"`` models, so ``model_dump(by_alias=True)`` round-trips
          straight into ``WidgetDefinition`` via the shared ``_coerce`` helper.

        Imports are local so the converter stays loadable without Beaver and the
        gateway-only boundary holds (mirrors ``dependencies.get_fox_validate``).

        Each lookup is memoized for the registry's lifetime (the instance is
        reused across conversions; Beaver's catalog is static for the process
        life). This collapses an N-widget screen's repeated widget types to one
        ``anyio.from_thread.run`` hop each instead of one per occurrence, and
        caches misses too. A bulk prefetch (one ``list_summaries`` pull) is the
        right move for batch/folder scale and is deferred to that follow-up.
        """
        from anyio.from_thread import run as run_async

        from canopy.beaver import gateway as beaver_gateway
        from canopy.beaver.errors import WidgetNotFound, QtClassNotMapped

        id_cache: dict[str, dict[str, Any] | None] = {}
        qt_cache: dict[str, dict[str, Any] | None] = {}

        def by_id_lookup(widget_id: str) -> dict[str, Any] | None:
            if widget_id not in id_cache:
                try:
                    id_cache[widget_id] = _to_payload(run_async(beaver_gateway.get, widget_id))
                except WidgetNotFound:
                    id_cache[widget_id] = None
            return id_cache[widget_id]

        def by_qt_class_lookup(qt_class: str) -> dict[str, Any] | None:
            if qt_class not in qt_cache:
                try:
                    qt_cache[qt_class] = _to_payload(run_async(beaver_gateway.by_qt_class, qt_class))
                except QtClassNotMapped:
                    qt_cache[qt_class] = None
            return qt_cache[qt_class]

        return cls(by_id_lookup=by_id_lookup, by_qt_class_lookup=by_qt_class_lookup)


def build_registry(config: ConverterConfig) -> RegistryClient:
    """Pick the widget registry per config: Beaver gateway, else vendored snapshot."""
    if config.use_beaver_gateway:
        return BeaverGatewayRegistry.from_canopy_gateway()
    return VendoredRegistry()
