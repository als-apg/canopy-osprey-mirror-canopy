"""Guard the shared themeable-colour definition in the widget registry.

Every colour prop in ``widget-registry-data/widgets/*.json`` carries the same
inline ``anyOf: [{type: string}, <ColorRole>]`` (no ``$ref``, no sibling
``type``), so a plain hex string and a ``{$role, fallback}`` object are both
valid. The vendored copy in the converter is a byte copy of these files, so
each file must also end in exactly one newline.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WIDGETS_DIR = _REPO_ROOT / "widget-registry-data" / "widgets"
_FILES = sorted(_WIDGETS_DIR.glob("*.json"))

_SAMPLE_ROLE = {"$role": "MAJOR", "fallback": "#ff0000", "fallbackDark": "#ff4444"}


def _is_color_name(name: str) -> bool:
    return name == "color" or name.endswith("Color")


def _color_props(widget: dict) -> list[tuple[str, dict]]:
    """Return (dotted path, prop schema) for every colour prop, recursing into array items."""
    found: list[tuple[str, dict]] = []

    def walk(props: dict, prefix: str) -> None:
        for name, schema in props.items():
            path = f"{prefix}{name}"
            if _is_color_name(name):
                found.append((path, schema))
            elif isinstance(schema, dict):
                items = schema.get("items")
                if isinstance(items, dict) and "properties" in items:
                    walk(items["properties"], f"{path}.items.")

    walk(widget.get("propSchema", {}).get("properties", {}), "")
    return found


_CASES = [
    pytest.param(p.stem, path, schema, id=f"{p.stem}:{path}")
    for p in _FILES
    for path, schema in _color_props(json.loads(p.read_text()))
]


def test_collected_colour_props() -> None:
    assert len(_FILES) >= 30
    assert len(_CASES) >= 24
    names = {c.values[1] for c in _CASES}
    assert "curves.items.color" in names
    assert "backgroundColor" in names


@pytest.mark.parametrize(("widget", "path", "schema"), _CASES)
def test_colour_prop_uses_shared_anyof(widget: str, path: str, schema: dict) -> None:
    reference = _CASES[0].values[2]["anyOf"]
    assert "type" not in schema
    assert "$ref" not in json.dumps(schema)
    assert schema["anyOf"] == reference


def test_anyof_shape() -> None:
    any_of = _CASES[0].values[2]["anyOf"]
    assert any_of[0] == {"type": "string"}
    assert any_of[1]["required"] == ["$role", "fallback"]
    assert any_of[1]["additionalProperties"] is False


@pytest.mark.parametrize(("widget", "path", "schema"), _CASES)
def test_sample_values_validate(widget: str, path: str, schema: dict) -> None:
    validator = jsonschema.Draft7Validator(schema)
    jsonschema.Draft7Validator.check_schema(schema)
    assert validator.is_valid(_SAMPLE_ROLE)
    assert validator.is_valid({"$role": "Text", "fallback": "#112233"})
    assert validator.is_valid("#abcdef")


@pytest.mark.parametrize(("widget", "path", "schema"), _CASES)
def test_bad_role_rejected(widget: str, path: str, schema: dict) -> None:
    validator = jsonschema.Draft7Validator(schema)
    assert not validator.is_valid({"$role": "X", "fallback": "red"})
    assert not validator.is_valid({"$role": "X", "fallback": "#ff0000", "extra": 1})
    assert not validator.is_valid({"fallback": "#ff0000"})
    assert not validator.is_valid(5)


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.stem)
def test_single_final_newline(path: Path) -> None:
    data = path.read_bytes()
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")
