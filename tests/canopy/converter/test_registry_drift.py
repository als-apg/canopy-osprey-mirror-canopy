"""Drift guard between pydmconverter's vendored widget registry and canopy's.

pydmconverter ships a snapshot of the widget registry
(``pydmconverter.ir.data.widget-registry``) that it uses to lower Qt
properties into Screen-IR props when no live registry is available. canopy
owns the authoritative copy in ``widget-registry-data/widgets``. If the two
disagree on a widget's ``qtPropMap``, a screen converted offline carries
different props than one converted against canopy, and nothing else notices.

For every widget id in the vendored snapshot this test asserts that canopy
has the same widget, that both ``qtPropMap`` key sets are equal, and that each
key maps to the same ``to`` target. Parametrised per id so a failure names
the widget. Both inputs ship with the code, so the test is unconditional.
"""

import json
from importlib import resources
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANOPY_WIDGETS_DIR = _REPO_ROOT / "widget-registry-data" / "widgets"


def _vendored_dir():
    return resources.files("pydmconverter.ir.data").joinpath("widget-registry")


def _vendored_ids() -> list[str]:
    return sorted(
        entry.name.removesuffix(".json") for entry in _vendored_dir().iterdir() if entry.name.endswith(".json")
    )


def _qt_prop_map(text: str) -> dict:
    return json.loads(text).get("qtPropMap") or {}


_VENDORED_IDS = _vendored_ids()


def test_vendored_snapshot_is_not_empty():
    # Guards against the parametrised test silently collecting zero cases.
    assert _VENDORED_IDS


def test_no_vendored_id_missing_from_canopy():
    missing = [wid for wid in _VENDORED_IDS if not (_CANOPY_WIDGETS_DIR / f"{wid}.json").is_file()]
    assert missing == [], f"vendored widget ids absent from canopy: {missing}"


@pytest.mark.parametrize("widget_id", _VENDORED_IDS)
def test_qt_prop_map_matches(widget_id: str):
    canopy_file = _CANOPY_WIDGETS_DIR / f"{widget_id}.json"
    assert canopy_file.is_file(), f"{widget_id}: missing from canopy widget-registry-data"

    vendored = _qt_prop_map(_vendored_dir().joinpath(f"{widget_id}.json").read_text(encoding="utf-8"))
    canopy = _qt_prop_map(canopy_file.read_text(encoding="utf-8"))

    assert set(vendored) == set(canopy), (
        f"{widget_id}: qtPropMap keys differ; "
        f"only vendored={sorted(set(vendored) - set(canopy))}, "
        f"only canopy={sorted(set(canopy) - set(vendored))}"
    )
    mismatched = {
        key: (vendored[key].get("to"), canopy[key].get("to"))
        for key in vendored
        if vendored[key].get("to") != canopy[key].get("to")
    }
    assert mismatched == {}, f"{widget_id}: qtPropMap 'to' targets differ (vendored, canopy): {mismatched}"
