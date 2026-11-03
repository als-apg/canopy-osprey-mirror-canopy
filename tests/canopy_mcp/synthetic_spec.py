"""A synthetic spec staging directory, written into a test's ``tmp_path`` at run time.

No schema file is tracked in canopy (the IR schema and the prop schemas belong
to canopy-spec), so the spec-present path is tested on these stand-ins. They
keep the shape the check pass relies on: a draft 2020-12 IR schema per version
with a closed, recursive ``WidgetNode`` (``meta`` and ``warnings`` allowed),
a two-armed ``Rule`` (conditional or formula), a 1.0 ``schemaVersion`` const and a
1.1 enum, theme colour roles (``{"$role", "fallback"}``) in 1.1 only, and
draft-07 per-widget prop schemas in the ``<dir>/widgets/<ir id>.props.schema.json``
layout of the epic staging directory; the plot types carry strict ``curves.items``.
"""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path

SPEC_SHA = "0123456789abcdef0123456789abcdef01234567"

_STRING = {"type": "string"}
_HEX = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
COLOR_ROLE: dict[str, Any] = {
    "type": "object",
    "required": ["$role", "fallback"],
    "additionalProperties": False,
    "properties": {"$role": {"type": "string", "minLength": 1}, "fallback": _HEX, "fallbackDark": _HEX},
}
#: The inline colour def a 1.1 colour prop carries: a plain colour or a theme role.
COLOR_DEF: dict[str, Any] = {"anyOf": [_STRING, COLOR_ROLE]}

_TRACE_CURVE: dict[str, Any] = {
    "y_channel": {"type": "string", "minLength": 1},
    "x_channel": {"type": ["string", "null"]},
    "name": _STRING,
    "color": COLOR_DEF,
    "lineStyle": {"type": "integer"},
    "lineWidth": {"type": "number"},
    "yAxisName": _STRING,
}
#: The closed ``curves[]`` item of each plot type: one required channel key, no other key allowed.
PLOT_CURVE_ITEMS: dict[str, dict[str, Any]] = {
    "waveform-plot": {
        "type": "object",
        "required": ["y_channel"],
        "additionalProperties": False,
        "properties": {**_TRACE_CURVE},
    },
    "xy-plot": {
        "type": "object",
        "required": ["y_channel"],
        "additionalProperties": False,
        "properties": {
            **_TRACE_CURVE,
            "symbol": _STRING,
            "symbolSize": {"type": "number"},
            "traceType": _STRING,
        },
    },
    "pv-array-plot": {
        "type": "object",
        "required": ["y_channels"],
        "additionalProperties": False,
        "properties": {
            "y_channels": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
            "name": _STRING,
            "color": COLOR_DEF,
        },
    },
}
#: The draft-07 prop schema ``write_spec`` stages for each plot type.
PLOT_PROPS: dict[str, dict[str, Any]] = {
    widget_id: {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"curves": {"type": "array", "items": items}},
    }
    for widget_id, items in PLOT_CURVE_ITEMS.items()
}


def ir_schema(version: str) -> dict[str, Any]:
    """A stand-in Screen-IR schema for ``version`` ("1.0" or "1.1")."""
    roles = version != "1.0"
    scalar: dict[str, Any] = {"type": ["string", "number", "boolean", "null"]}
    # A conditional rule's default and condition values: a scalar, or (1.1) a theme colour role.
    value: dict[str, Any] = {"anyOf": [scalar, {"$ref": "#/$defs/ColorRole"}]} if roles else scalar
    defs: dict[str, Any] = {
        "Geometry": {
            "type": "object",
            "required": ["x", "y", "width", "height"],
            "properties": {k: {"type": "number"} for k in ("x", "y", "width", "height")},
        },
        # Two arms, as in the real schema: conditional (pvs + conditions) or formula.
        "Rule": {
            "type": "object",
            "oneOf": [
                {
                    "required": ["id", "targetProperty", "conditions"],
                    "additionalProperties": False,
                    "properties": {
                        "id": _STRING,
                        "name": _STRING,
                        "targetProperty": _STRING,
                        "default": value,
                        "pvs": {"type": "array"},
                        "conditions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["expression", "value"],
                                "additionalProperties": False,
                                "properties": {"expression": _STRING, "value": value},
                            },
                        },
                    },
                },
                {
                    "required": ["id", "targetProperty", "formula"],
                    "additionalProperties": False,
                    "properties": {
                        "id": _STRING,
                        "name": _STRING,
                        "targetProperty": _STRING,
                        "default": scalar,
                        "formula": _STRING,
                    },
                },
            ],
        },
        "WidgetNode": {
            "type": "object",
            "required": ["id", "type", "geometry"],
            "additionalProperties": False,
            "properties": {
                "id": _STRING,
                "type": _STRING,
                "name": _STRING,
                "props": {"type": "object"},
                "geometry": {"$ref": "#/$defs/Geometry"},
                "children": {"type": "array", "items": {"$ref": "#/$defs/WidgetNode"}},
                "rules": {"type": "array", "items": {"$ref": "#/$defs/Rule"}},
                "meta": {"type": "object"},
                "warnings": {"type": "array", "items": _STRING},
            },
        },
    }
    if roles:
        defs["ColorRole"] = COLOR_ROLE
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://canopy.test/screen-ir/{version}/schema.json",
        "type": "object",
        "required": ["schemaVersion", "kind", "id", "root"],
        "properties": {
            "schemaVersion": {"enum": ["1.0", "1.1"]} if roles else {"const": "1.0"},
            "kind": {"enum": ["screen"]},
            "id": _STRING,
            "metadata": {
                "type": "object",
                "properties": {
                    "size": {
                        "type": "object",
                        "properties": {"width": {"type": "number"}, "height": {"type": "number"}},
                    }
                },
            },
            "macros": {"type": "array"},
            "formulas": {"type": "array"},
            "root": {"$ref": "#/$defs/WidgetNode"},
        },
        "$defs": defs,
    }


def write_spec(
    root: Path,
    *,
    versions: tuple[str, ...] = ("1.0", "1.1"),
    props: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write a staging directory at ``root``: the IR schema of each version, the prop schemas, SPEC-SOURCE.

    The plot types carry strict ``curves.items`` (``PLOT_PROPS``); a schema in ``props`` for the same id wins.
    """
    (root / "ir").mkdir(parents=True, exist_ok=True)
    (root / "widgets").mkdir(exist_ok=True)
    for version in versions:
        (root / "ir" / f"screen-ir-{version}.schema.json").write_text(json.dumps(ir_schema(version)))
    for widget_id, schema in {**PLOT_PROPS, **(props or {})}.items():
        (root / "widgets" / f"{widget_id}.props.schema.json").write_text(json.dumps(schema))
    (root / "SPEC-SOURCE").write_text(f"repo: slaclab/canopy-spec\nsha: {SPEC_SHA}\n")
    return root
