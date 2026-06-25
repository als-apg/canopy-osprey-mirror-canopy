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


def test_load_missing_widgets_dir_serves_empty_catalog(tmp_path):
    """A missing data dir must not fail the composed app's startup —
    Beaver degrades to an empty catalog like Mole/Raccoon do when unconfigured."""
    cfg = _config_for(tmp_path / "missing")
    index = FileCatalogLoader(cfg).load()
    assert index.widget_count == 0
    assert index.by_category == {}
    assert index.registry_version  # still a stable hash


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


def test_load_rejects_invalid_prop_schema(tmp_catalog, fixture_widgets):
    """A propSchema that is not itself a valid JSON Schema fails at startup,
    not as a 500 on the first /validate call."""
    bad = dict(fixture_widgets[0])
    bad["id"] = "bad-schema"
    bad["qtMapping"] = {"class": "PyDMBadSchema"}
    bad["propSchema"] = {"type": "object", "required": "pv"}  # required must be an array
    bad["inspectorSchema"] = {"groups": []}
    (tmp_catalog / "widgets" / "bad-schema.json").write_text(json.dumps(bad), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="not a valid JSON Schema"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_inspector_field_without_backing_prop(tmp_catalog, fixture_widgets):
    bad = dict(fixture_widgets[0])
    bad["id"] = "dangling-field"
    bad["qtMapping"] = {"class": "PyDMDangling"}
    bad["inspectorSchema"] = {"groups": [{"name": "PV", "fields": ["pv", "ghost"]}]}
    (tmp_catalog / "widgets" / "dangling.json").write_text(json.dumps(bad), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="inspectorSchema field 'ghost'"):
        FileCatalogLoader(cfg).load()


def test_load_rejects_missing_palette_icon(tmp_catalog, fixture_widgets):
    bad = dict(fixture_widgets[0])
    bad["id"] = "iconless"
    bad["qtMapping"] = {"class": "PyDMIconless"}
    bad["paletteIcon"] = "ghost.svg"
    (tmp_catalog / "widgets" / "iconless.json").write_text(json.dumps(bad), encoding="utf-8")
    cfg = _config_for(tmp_catalog / "widgets")
    with pytest.raises(CatalogMisconfigured, match="paletteIcon 'ghost.svg' not found"):
        FileCatalogLoader(cfg).load()


def test_load_precompiles_prop_validators(tmp_catalog, fixture_widgets):
    cfg = _config_for(tmp_catalog / "widgets")
    index = FileCatalogLoader(cfg).load()
    assert set(index.validators) == {w["id"] for w in fixture_widgets}
