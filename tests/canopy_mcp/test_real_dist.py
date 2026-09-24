"""The widget catalog join against the REAL viewer build, not the test fixture.

Runs only when ``CANOPY_VIEWER_DIST`` names a built ``apps/viewer/dist``; the
fixture-based twin of this test lives in ``test_catalog.py``.
"""

import os
import json
from pathlib import Path

import pytest

from canopy_mcp.catalog import IR_MAP_FILENAME, PLACEHOLDER_TYPE, load_ir_map, build_catalog, join_coverage

_DIST_ENV = os.environ.get("CANOPY_VIEWER_DIST")

pytestmark = pytest.mark.skipif(not _DIST_ENV, reason="CANOPY_VIEWER_DIST not set; real viewer dist unavailable")

REGISTRY_DIR = Path(__file__).resolve().parents[2] / "widget-registry-data" / "widgets"


@pytest.fixture(scope="module")
def dist() -> Path:
    path = Path(_DIST_ENV or "")
    if not (path / IR_MAP_FILENAME).is_file():
        pytest.fail(f"CANOPY_VIEWER_DIST={path} has no {IR_MAP_FILENAME}; build the viewer first")
    return path


def _registry_details() -> list[dict]:
    """The checked-in Beaver widget files, as GET /beaver/v1/widgets/{id} returns them."""
    return [json.loads(p.read_text()) for p in sorted(REGISTRY_DIR.glob("*.json"))]


def test_real_ir_map_joins_every_beaver_id_and_every_ir_type_has_an_entry(dist: Path) -> None:
    details = _registry_details()
    beaver_ids = [d["id"] for d in details]
    assert beaver_ids, "no registry data under widget-registry-data/widgets"

    ir_map = load_ir_map(dist)
    assert join_coverage(beaver_ids, list(ir_map)) == []

    catalog = build_catalog(details, ir_map)
    assert catalog.unmapped_beaver_ids == ()
    assert set(ir_map) <= set(catalog.entries)
    # unknown-widget is the ONE type outside both sources.
    assert set(catalog.entries) - set(ir_map) - set(beaver_ids) == {PLACEHOLDER_TYPE}


def test_real_ir_map_types_beaver_does_not_document_are_all_verbatim(dist: Path) -> None:
    ir_map = load_ir_map(dist)
    beaver_ids = {d["id"] for d in _registry_details()}
    catalog = build_catalog(_registry_details(), ir_map)
    for type_ in set(ir_map) - beaver_ids:
        assert ir_map[type_].get("verbatim") is True, type_
        assert catalog.get(type_).undocumented
