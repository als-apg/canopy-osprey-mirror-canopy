"""Smoke test pinning that the shipped ``widget-registry-data`` loads through
the real ``FileCatalogLoader``.

Every other Beaver test loads a tmp-dir fixture catalog; this one points the
loader at the actual on-disk seed data so a bad widget def or a missing icon
file fails here, at test time, instead of at ``canopy-api`` startup.
"""

from pathlib import Path

from canopy.beaver.config import BeaverConfig
from canopy.beaver.catalog.loader import FileCatalogLoader

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WIDGETS_DIR = _REPO_ROOT / "widget-registry-data" / "widgets"


def _seed_config() -> BeaverConfig:
    return BeaverConfig(
        widgets_dir=_WIDGETS_DIR,
        icons_dir=_REPO_ROOT / "widget-registry-data" / "icons",
        legacy_screens_dir=_REPO_ROOT / "widget-registry-data" / "legacy-screens",
    )


# Hardcoded so an accidental add/deletion of a seed widget def fails here rather
# than passing tautologically against the same directory the loader reads. Bump
# this when the seed widget set changes.
_EXPECTED_WIDGET_COUNT = 31


def test_seed_catalog_loads():
    index = FileCatalogLoader(_seed_config()).load()
    assert index.widget_count == _EXPECTED_WIDGET_COUNT


def test_seed_catalog_includes_new_drawing_and_shape_widgets():
    index = FileCatalogLoader(_seed_config()).load()
    assert {"rectangle", "ellipse", "line", "arc", "group", "pv-meter"} <= set(index.by_id)


def test_seed_catalog_qt_class_mappings():
    index = FileCatalogLoader(_seed_config()).load()
    assert index.by_qt_class["PyDMEnumButton"].id == "pv-radio-group"
    assert index.by_qt_class["PyDMDrawingRectangle"].id == "rectangle"
    assert index.by_qt_class["PyDMAnalogIndicator"].id == "pv-meter"


def test_seed_catalog_drawing_category_membership():
    index = FileCatalogLoader(_seed_config()).load()
    assert {w.id for w in index.by_category["drawing"]} == {"rectangle", "ellipse", "line", "arc", "polygon"}


def test_seed_catalog_extended_prop_schemas():
    index = FileCatalogLoader(_seed_config()).load()
    assert "foregroundColor" in index.by_id["text-label"].prop_schema["properties"]
    assert "foregroundColor" in index.by_id["pv-label"].prop_schema["properties"]
    assert "actions" in index.by_id["regular-button"].prop_schema["properties"]
