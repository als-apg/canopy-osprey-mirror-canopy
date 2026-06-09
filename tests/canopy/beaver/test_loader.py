"""Catalog loader: meta-schema validation + collision detection."""

import json
from pathlib import Path

import pytest

from canopy.beaver.config import BeaverConfig
from canopy.beaver.errors import CatalogMisconfigured
from canopy.beaver.catalog.loader import FileCatalogLoader


def _config_for(widgets_dir: Path) -> BeaverConfig:
    return BeaverConfig(
        widgets_dir=widgets_dir,
        icons_dir=widgets_dir.parent / "icons",
        legacy_screens_dir=widgets_dir.parent / "legacy-screens",
    )


def test_load_indexes_widgets(tmp_catalog, fixture_widgets):
    cfg = _config_for(tmp_catalog / "widgets")
    index = FileCatalogLoader(cfg).load()
    assert index.widget_count == len(fixture_widgets)
    assert {w.id for w in index.by_id.values()} == {w["id"] for w in fixture_widgets}


def test_load_builds_qt_class_index(tmp_catalog):
    cfg = _config_for(tmp_catalog / "widgets")
    index = FileCatalogLoader(cfg).load()
    assert "PyDMLabel" in index.by_qt_class
    assert index.by_qt_class["PyDMLabel"].id == "pv-label"


def test_load_groups_by_category(tmp_catalog):
    cfg = _config_for(tmp_catalog / "widgets")
    index = FileCatalogLoader(cfg).load()
    assert {"indicators", "controls"} <= set(index.by_category)


def test_load_rejects_unknown_widgets_dir(tmp_path):
    cfg = _config_for(tmp_path / "missing")
    with pytest.raises(CatalogMisconfigured, match="widgets_dir does not exist"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_malformed_json(tmp_catalog):
    bad = tmp_catalog / "widgets" / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="failed to read"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_meta_schema_violation(tmp_catalog):
    bad = tmp_catalog / "widgets" / "incomplete.json"
    bad.write_text(json.dumps({"id": "incomplete"}), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="fails meta-schema"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_duplicate_id(tmp_catalog, fixture_widgets):
    dup = dict(fixture_widgets[0])
    (tmp_catalog / "widgets" / "duplicate.json").write_text(json.dumps(dup), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="duplicate widget id"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_duplicate_qt_class(tmp_catalog, fixture_widgets):
    other = dict(fixture_widgets[0])
    other["id"] = "another-pv-label"
    (tmp_catalog / "widgets" / "another.json").write_text(json.dumps(other), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="duplicate qtMapping.class"):
        FileCatalogLoader(cfg).load()


def test_registry_version_is_deterministic(tmp_catalog):
    cfg = _config_for(tmp_catalog / "widgets")
    a = FileCatalogLoader(cfg).load()
    b = FileCatalogLoader(cfg).load()
    assert a.registry_version == b.registry_version
