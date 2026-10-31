"""Generate ``speed-100-50-20.screen.json``: the ``check`` timing fixture.

100 widget nodes (a ``group`` root and 99 children), 50 rules (30 conditional,
20 formula-arm) and 20 formulas with distinct expressions. Everything is derived
from loop indices -- no randomness -- so a re-run reproduces the checked-in file
byte for byte.

    uv run python tests/canopy_mcp/fixtures/gen_speed_fixture.py          # write
    uv run python tests/canopy_mcp/fixtures/gen_speed_fixture.py --check  # verify
"""

from __future__ import annotations

import sys
import json
from typing import Any
from pathlib import Path

FIXTURE = Path(__file__).with_name("speed-100-50-20.screen.json")

NODES = 100
RULES = 50
FORMULAS = 20
COLUMNS = 10
CELL_W, CELL_H = 120, 30


def _pv(i: int) -> str:
    return f"SR:C{i % 12 + 1:02d}:BPM{i % 9 + 1}:{'XY'[i % 2]}"


def _formula(i: int) -> dict[str, Any]:
    return {
        "name": f"f{i:02d}",
        "kind": "scalar",
        "expression": f"({{a}} - {{b}}) * {i + 1}",
        "bindings": {"a": _pv(2 * i), "b": _pv(2 * i + 1)},
    }


def _rule(i: int) -> dict[str, Any]:
    base = {"id": f"r{i:02d}", "name": f"rule {i}", "targetProperty": "visible", "default": True}
    if i < FORMULAS:
        return {**base, "formula": f"fox://f{i:02d}"}
    return {
        **base,
        "pvs": [{"name": _pv(i), "trigger": True}],
        "conditions": [{"expression": f"pv0 > {i}", "value": False}],
    }


def _child(i: int) -> dict[str, Any]:
    kind = i % 3
    if kind == 0:
        ntype, props = "text-label", {"text": f"Label {i}"}
    elif kind == 1:
        ntype, props = "pv-label", {"pv": _pv(i)}
    else:
        ntype, props = "rectangle", {}
    node: dict[str, Any] = {
        "id": f"n{i:03d}",
        "type": ntype,
        "props": props,
        "geometry": {"x": (i % COLUMNS) * CELL_W, "y": (i // COLUMNS) * CELL_H, "width": CELL_W, "height": CELL_H},
    }
    if i < RULES:
        node["rules"] = [_rule(i)]
    return node


def build() -> dict[str, Any]:
    children = [_child(i) for i in range(NODES - 1)]
    rows = (NODES - 2) // COLUMNS + 1
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "speed-100-50-20",
        "metadata": {
            "title": "check speed fixture",
            "source": {"type": "native"},
            "size": {"width": COLUMNS * CELL_W, "height": rows * CELL_H},
        },
        "macros": [],
        "formulas": [_formula(i) for i in range(FORMULAS)],
        "root": {
            "id": "root",
            "type": "group",
            "props": {},
            "geometry": {"x": 0, "y": 0, "width": COLUMNS * CELL_W, "height": rows * CELL_H},
            "children": children,
        },
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if FIXTURE.read_text(encoding="utf-8") != text:
            print(f"{FIXTURE.name} is out of date; re-run without --check", file=sys.stderr)
            return 1
        return 0
    FIXTURE.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
