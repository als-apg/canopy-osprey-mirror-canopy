"""Beaver-scoped pytest fixtures.

The ``beaver_client`` fixture follows the same dependency-override pattern as
Mole's: the override goes on the mounted Beaver sub-app, not the parent
``canopy.main:app``, because mounted apps maintain their own
``dependency_overrides`` table.
"""

import json
from typing import Any
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopy.main import app as parent_app
from canopy.beaver.app import app as beaver_app
from canopy.beaver.config import BeaverConfig
from canopy.beaver.dependencies import get_widget_service
from canopy.beaver.catalog.index import WidgetIndex
from canopy.beaver.catalog.loader import FileCatalogLoader
from canopy.beaver.services.widget_service import WidgetService


def _label_widget() -> dict[str, Any]:
    return {
        "id": "pv-label",
        "version": "1.0.0",
        "displayName": "PV Label",
        "description": "Displays a PV value as text.",
        "category": "indicators",
        "paletteIcon": "pv-label.svg",
        "reactComponent": {"name": "PVLabel", "module": "@canopy/widgets"},
        "qtMapping": {"class": "PyDMLabel", "extends": "QLabel"},
        "propSchema": {
            "type": "object",
            "required": ["pv"],
            "properties": {
                "pv": {"type": "string"},
                "precision": {"type": "integer", "minimum": 0},
            },
        },
        "qtPropMap": {
            "channel": {"to": "pv", "transform": "stripProtocol"},
        },
        "inspectorSchema": {"groups": [{"name": "PV", "fields": ["pv", "precision"]}]},
        "defaults": {"width": 100, "height": 24},
        "supportsPv": True,
        "supportsRules": True,
        "supportsChildren": False,
    }


def _button_widget() -> dict[str, Any]:
    return {
        "id": "pv-button",
        "version": "1.0.0",
        "displayName": "PV Button",
        "description": "Press/release values written to a PV.",
        "category": "controls",
        "paletteIcon": "pv-button.svg",
        "reactComponent": {"name": "PVButton", "module": "@canopy/widgets"},
        "qtMapping": {"class": "PyDMPushButton"},
        "propSchema": {"type": "object", "properties": {"pv": {"type": "string"}}},
        "inspectorSchema": {"groups": [{"name": "PV", "fields": ["pv"]}]},
        "supportsPv": True,
        "supportsRules": True,
        "supportsChildren": False,
    }


@pytest.fixture
def fixture_widgets() -> list[dict[str, Any]]:
    return [_label_widget(), _button_widget()]


@pytest.fixture
def tmp_catalog(tmp_path: Path, fixture_widgets) -> Path:
    """Tmp catalog tree with widgets/, icons/, legacy-screens/ subdirs."""
    widgets_dir = tmp_path / "widgets"
    widgets_dir.mkdir()
    for w in fixture_widgets:
        (widgets_dir / f"{w['id']}.json").write_text(json.dumps(w), encoding="utf-8")
    (tmp_path / "icons").mkdir()
    (tmp_path / "legacy-screens").mkdir()
    return tmp_path


@pytest.fixture
def beaver_config(tmp_catalog: Path) -> BeaverConfig:
    return BeaverConfig(
        widgets_dir=tmp_catalog / "widgets",
        icons_dir=tmp_catalog / "icons",
        legacy_screens_dir=tmp_catalog / "legacy-screens",
    )


@pytest.fixture
def beaver_index(beaver_config: BeaverConfig) -> WidgetIndex:
    return FileCatalogLoader(beaver_config).load()


@pytest.fixture
def beaver_service(beaver_index: WidgetIndex, beaver_config: BeaverConfig) -> WidgetService:
    return WidgetService(beaver_index, beaver_config)


@pytest.fixture
def beaver_client(beaver_service: WidgetService):
    """TestClient with the Beaver service swapped via dependency_overrides on the sub-app."""
    beaver_app.dependency_overrides[get_widget_service] = lambda: beaver_service
    try:
        with TestClient(parent_app) as client:
            yield client
    finally:
        beaver_app.dependency_overrides.pop(get_widget_service, None)
