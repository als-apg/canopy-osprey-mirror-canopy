"""Unit tests for the Beaver registry adapter + registry selection (Seam #1)."""

import anyio
import pytest
from pydmconverter.ir.registry import VendoredRegistry, WidgetDefinition

from canopy.beaver import gateway as beaver_gateway
from canopy.beaver.errors import WidgetNotFound, QtClassNotMapped
from canopy.converter.config import ConverterConfig
from canopy.beaver.schemas.widget import (
    QtMapping,
    WidgetDTO,
    InspectorSchema,
    ReactComponentRef,
)
from canopy.converter.adapters.beaver_registry import (
    BeaverGatewayRegistry,
    build_registry,
)

# A Beaver payload carries more than the converter reads; extras must be ignored.
_PAYLOAD = {"id": "pv-label", "qtMapping": {"class": "PyDMLabel"}, "paletteIcon": "x", "extraBeaverField": 123}


def test_maps_beaver_payload_to_widget_definition():
    reg = BeaverGatewayRegistry(
        by_id_lookup=lambda i: _PAYLOAD if i == "pv-label" else None,
        by_qt_class_lookup=lambda c: _PAYLOAD if c == "PyDMLabel" else None,
    )
    definition = reg.by_id("pv-label")
    assert isinstance(definition, WidgetDefinition)
    assert definition.id == "pv-label"
    assert definition.qt_class == "PyDMLabel"  # derived from qtMapping; extras ignored
    assert reg.by_qt_class("PyDMLabel").id == "pv-label"


def test_registry_miss_returns_none():
    reg = BeaverGatewayRegistry(by_id_lookup=lambda i: None, by_qt_class_lookup=lambda c: None)
    assert reg.by_id("nope") is None
    assert reg.by_qt_class("nope") is None


def test_build_registry_defaults_to_vendored_snapshot():
    registry = build_registry(ConverterConfig(use_beaver_gateway=False))
    assert isinstance(registry, VendoredRegistry)


# --- Live Beaver gateway path (use_beaver_gateway=true) -------------------------

# A full WidgetDTO as Beaver returns it: qt_prop_map defaults to None, which the
# adapter must drop (WidgetDefinition.qtPropMap is a non-optional dict).
_DTO = WidgetDTO(
    id="pv-label",
    version="1.0.0",
    display_name="Label",
    description="A PV label",
    category="indicators",
    palette_icon="label.svg",
    react_component=ReactComponentRef(name="PVLabel", module="@canopy/widgets"),
    qt_mapping=QtMapping(**{"class": "PyDMLabel"}),
    inspector_schema=InspectorSchema(groups=[]),
    supports_pv=True,
    supports_rules=True,
    supports_children=False,
)


@pytest.fixture
def stub_beaver_gateway(monkeypatch):
    """Patch the async Beaver gateway: ``pv-label`` / ``PyDMLabel`` hit, else miss."""

    async def fake_get(widget_id: str) -> WidgetDTO:
        if widget_id == "pv-label":
            return _DTO
        raise WidgetNotFound(f"No widget registered with id {widget_id!r}")

    async def fake_by_qt_class(qt_class: str) -> WidgetDTO:
        if qt_class == "PyDMLabel":
            return _DTO
        raise QtClassNotMapped(f"No widget mapped from Qt class {qt_class!r}")

    monkeypatch.setattr(beaver_gateway, "get", fake_get)
    monkeypatch.setattr(beaver_gateway, "by_qt_class", fake_by_qt_class)


def test_from_canopy_gateway_builds_registry(stub_beaver_gateway):
    assert isinstance(BeaverGatewayRegistry.from_canopy_gateway(), BeaverGatewayRegistry)


async def test_live_gateway_hit_coerces_real_widget_dto(stub_beaver_gateway):
    # by_id/by_qt_class are sync and call back into the event loop via
    # anyio.from_thread.run, so they must run from a worker thread (the real
    # path: the route does run_in_threadpool(service.convert)).
    reg = BeaverGatewayRegistry.from_canopy_gateway()

    by_id = await anyio.to_thread.run_sync(reg.by_id, "pv-label")
    assert isinstance(by_id, WidgetDefinition)
    assert by_id.id == "pv-label"
    assert by_id.qt_class == "PyDMLabel"
    assert by_id.supports_pv is True
    assert by_id.qt_prop_map == {}  # Beaver's None dropped; default applies

    by_qt = await anyio.to_thread.run_sync(reg.by_qt_class, "PyDMLabel")
    assert by_qt.id == "pv-label"


async def test_live_gateway_miss_returns_none(stub_beaver_gateway):
    reg = BeaverGatewayRegistry.from_canopy_gateway()
    assert await anyio.to_thread.run_sync(reg.by_id, "no-such-widget") is None
    assert await anyio.to_thread.run_sync(reg.by_qt_class, "NoSuchQtClass") is None


def test_build_registry_with_beaver_flag_uses_gateway(stub_beaver_gateway):
    registry = build_registry(ConverterConfig(use_beaver_gateway=True))
    assert isinstance(registry, BeaverGatewayRegistry)


async def test_live_gateway_memoizes_lookups(monkeypatch):
    """Repeated lookups (hits and misses) hit Beaver once, then serve from cache."""
    calls = {"get": 0}

    async def counting_get(widget_id: str) -> WidgetDTO:
        calls["get"] += 1
        if widget_id == "pv-label":
            return _DTO
        raise WidgetNotFound(widget_id)

    monkeypatch.setattr(beaver_gateway, "get", counting_get)
    reg = BeaverGatewayRegistry.from_canopy_gateway()

    # Same hit twice -> one gateway call.
    assert (await anyio.to_thread.run_sync(reg.by_id, "pv-label")).id == "pv-label"
    assert (await anyio.to_thread.run_sync(reg.by_id, "pv-label")).id == "pv-label"
    assert calls["get"] == 1

    # Misses are cached too -> still one additional call for a repeated miss.
    assert await anyio.to_thread.run_sync(reg.by_id, "missing") is None
    assert await anyio.to_thread.run_sync(reg.by_id, "missing") is None
    assert calls["get"] == 2
