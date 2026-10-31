"""The widget catalog built from the REAL viewer build, not the test fixture.

Runs only when ``CANOPY_VIEWER_DIST`` names a built ``apps/viewer/dist``; the
fixture-based twin of these tests lives in ``test_catalog.py``. The real
``ir-map.json`` is the whole catalog: every entry carries the editor metadata
(``displayName``, ``description``, ``category``, ``supportsChildren``) the
runtime emits beside ``consumes`` and ``verbatim``.
"""

import os
from pathlib import Path

import pytest

from canopy_mcp.catalog import (
    IR_MAP_FILENAME,
    PLACEHOLDER_TYPE,
    load_ir_map,
    build_catalog,
)

_DIST_ENV = os.environ.get("CANOPY_VIEWER_DIST")

pytestmark = pytest.mark.skipif(not _DIST_ENV, reason="CANOPY_VIEWER_DIST not set; real viewer dist unavailable")


@pytest.fixture(scope="module")
def dist() -> Path:
    path = Path(_DIST_ENV or "")
    if not (path / IR_MAP_FILENAME).is_file():
        pytest.fail(f"CANOPY_VIEWER_DIST={path} has no {IR_MAP_FILENAME}; build the viewer first")
    return path


def test_real_ir_map_entries_all_carry_editor_metadata(dist: Path) -> None:
    ir_map = load_ir_map(dist)
    for widget_type, entry in ir_map.items():
        assert isinstance(entry.get("displayName"), str) and entry["displayName"], widget_type
        assert isinstance(entry.get("description"), str) and entry["description"], widget_type
        assert isinstance(entry.get("category"), str) and entry["category"], widget_type
        assert isinstance(entry.get("supportsChildren"), bool), widget_type
    labels = [entry["displayName"] for entry in ir_map.values()]
    assert len(labels) == len(set(labels)), "displayName labels are not unique"


def test_real_catalog_is_the_ir_map_plus_the_placeholder(dist: Path) -> None:
    ir_map = load_ir_map(dist)
    catalog = build_catalog(ir_map)
    # unknown-widget is the ONE type outside the viewer's map.
    assert set(catalog.entries) - set(ir_map) == {PLACEHOLDER_TYPE}
    assert set(ir_map) <= set(catalog.entries)
    for widget_type, entry in ir_map.items():
        got = catalog.get(widget_type)
        assert got.display_name == entry["displayName"]
        assert got.category == entry["category"]
        assert got.description == entry["description"]
        assert got.supports_children is entry["supportsChildren"]
        assert got.consumes == tuple(entry.get("consumes") or [])


def test_real_ir_map_verbatim_types_are_undocumented_without_a_spec(dist: Path) -> None:
    ir_map = load_ir_map(dist)
    catalog = build_catalog(ir_map)
    assert any(entry.get("verbatim") for entry in ir_map.values()), "the real map has no verbatim type"
    for widget_type, entry in ir_map.items():
        assert catalog.get(widget_type).undocumented is bool(entry.get("verbatim")), widget_type
