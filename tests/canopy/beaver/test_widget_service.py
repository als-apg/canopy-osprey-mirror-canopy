"""WidgetService gateway-layer operations."""

import pytest

from canopy.beaver.errors import WidgetNotFound, QtClassNotMapped


async def test_get_returns_widget(beaver_service):
    w = await beaver_service.get("pv-label")
    assert w.id == "pv-label"


async def test_get_unknown_raises(beaver_service):
    with pytest.raises(WidgetNotFound):
        await beaver_service.get("does-not-exist")


async def test_by_qt_class_returns_widget(beaver_service):
    w = await beaver_service.by_qt_class("PyDMLabel")
    assert w.id == "pv-label"


async def test_by_qt_class_unknown_raises(beaver_service):
    with pytest.raises(QtClassNotMapped):
        await beaver_service.by_qt_class("PyDMNonexistent")


async def test_list_summaries_unfiltered(beaver_service):
    summaries = await beaver_service.list_summaries()
    assert {s.id for s in summaries} == {"pv-label", "pv-button"}


async def test_list_summaries_filtered_by_category(beaver_service):
    indicators = await beaver_service.list_summaries(category="indicators")
    assert [s.id for s in indicators] == ["pv-label"]


async def test_categories_returns_all_declared_with_counts(beaver_service):
    """Every declared category appears (empty ones with count 0), in
    declaration order, so the palette keeps stable section scaffolding."""
    cats = await beaver_service.categories()
    assert [c.id for c in cats] == ["indicators", "controls", "containers", "plots", "decorations", "legacy"]
    counts = {c.id: c.count for c in cats}
    assert counts == {
        "indicators": 1,
        "controls": 1,
        "containers": 0,
        "plots": 0,
        "decorations": 0,
        "legacy": 0,
    }


async def test_validate_props_pass(beaver_service):
    result = await beaver_service.validate_props("pv-label", {"pv": "TEST:PV", "precision": 3})
    assert result.valid is True
    assert result.errors == []


async def test_validate_props_fail(beaver_service):
    result = await beaver_service.validate_props("pv-label", {})
    assert result.valid is False
    assert len(result.errors) >= 1


async def test_validate_props_unknown_widget_raises(beaver_service):
    with pytest.raises(WidgetNotFound):
        await beaver_service.validate_props("nope", {})
