"""Meta-schema: JSON Schema that every widget definition file must satisfy.

The loader rejects any file that fails this validator before WidgetDTO type
validation is attempted; this keeps error messages closer to the user's
authored field names than a Pydantic ValidationError would.
"""

from typing import Any

META_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft-07/schema",
    "type": "object",
    "required": [
        "id",
        "version",
        "displayName",
        "description",
        "category",
        "paletteIcon",
        "reactComponent",
        "propSchema",
        "inspectorSchema",
        "supportsPv",
        "supportsRules",
        "supportsChildren",
    ],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
        "version": {"type": "string", "minLength": 1},
        "displayName": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "category": {
            "type": "string",
            "enum": ["indicators", "controls", "containers", "plots", "decorations", "legacy"],
        },
        "paletteIcon": {"type": "string", "minLength": 1},
        "reactComponent": {
            "type": "object",
            "required": ["name", "module"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "module": {"type": "string", "minLength": 1},
            },
        },
        "qtMapping": {
            "type": "object",
            "required": ["class"],
            "properties": {
                "class": {"type": "string", "minLength": 1},
                "extends": {"type": "string"},
                "header": {"type": "string"},
            },
        },
        "propSchema": {"type": "object"},
        "qtPropMap": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["to"],
                "properties": {
                    "to": {"type": "string", "minLength": 1},
                    "transform": {"type": "string"},
                },
            },
        },
        "inspectorSchema": {
            "type": "object",
            "required": ["groups"],
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "fields"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "fields": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "defaults": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
            },
        },
        "supportsPv": {"type": "boolean"},
        "supportsRules": {"type": "boolean"},
        "supportsChildren": {"type": "boolean"},
    },
}
