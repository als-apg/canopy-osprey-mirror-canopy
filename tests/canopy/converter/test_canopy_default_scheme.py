"""``canopy-default.json`` is the root colour scheme every facility scheme extends.

It is the single source for the role set, its Carbon-matched light (white theme)
and dark (g100) values, and the import maps that turn Phoebus named colours and
stock EDM ``colors.list`` names into roles. The PyDM converter vendors a byte
copy and the SDK ships a flattened copy, so the file must stay schema-valid and
byte-canonical.

The schema is owned by the SDK (``canopy-color-scheme-1.schema.json``). Its
checkout only exists on a developer machine, so the schema test reads sibling
roots from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list) and is
skipped when the variable is unset.
"""

import json
import os
import re
from pathlib import Path

import jsonschema
import pytest

import canopy.converter

SCHEME_PATH = Path(canopy.converter.__file__).parent / "data" / "schemes" / "canopy-default.json"

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"
SCHEMA_RELATIVE = "packages/screen-ir/schema/canopy-color-scheme-1.schema.json"

ROLES = [
    "Transparent",
    "OK",
    "MINOR",
    "MAJOR",
    "INVALID",
    "DISCONNECTED",
    "Text",
    "ActiveText",
    "Background",
    "Read_Background",
    "Write_Background",
    "Button_Background",
    "Grid",
    "Grid_Fill",
    "Display_Background",
    "On",
    "Off",
    "Header_Background",
    "Header_ForeGround",
    "Attention",
    "STOP",
]

# Phoebus standard named colours (color.def plus the NamedWidgetColors built-ins).
# Grid_Fill is not a Phoebus name: it is the box-background slot of Phoebus Grid.
PHOEBUS_NAMES = [name for name in ROLES if name not in ("Display_Background", "Grid_Fill")]

# Phoebus Grid splits by use: as a box background it becomes Grid_Fill.
GRID_IMPORT = {"role": "Grid", "background_color": "Grid_Fill"}

# Stock EDM colors.list names whose meaning matches a role.
EDM_IMPORT = {
    "Disconn/Invalid": "DISCONNECTED",
    "GLOBAL canvas": "Display_Background",
    "GLOBAL title": "Header_Background",
    "Monitor: NORMAL": "OK",
    "Monitor: MAJOR": "MAJOR",
    "Mon: MAJOR/unack": "MAJOR",
    "Monitor: MINOR": "MINOR",
    "Mon: MINOR/unack": "MINOR",
    "Exit/Quit/Kill": "STOP",
    "invisible": "Transparent",
}

# Today's runtime --pydm-alarm-* tokens; the light alarm roles must not drift.
ALARM_LIGHT = {
    "OK": "#24a148",
    "MINOR": "#f1c21b",
    "MAJOR": "#da1e28",
    "INVALID": "#a56eff",
    "DISCONNECTED": "#6f6f6f",
}

# Light values are today's CANOPY look and stay put; only dark was redesigned.
LIGHT = {
    "Transparent": "#ffffff00",
    "OK": "#24a148",
    "MINOR": "#f1c21b",
    "MAJOR": "#da1e28",
    "INVALID": "#a56eff",
    "DISCONNECTED": "#6f6f6f",
    "Text": "#161616",
    "ActiveText": "#0f62fe",
    "Background": "#ffffff",
    "Read_Background": "#f4f4f4",
    "Write_Background": "#edf5ff",
    "Button_Background": "#e0e0e0",
    "Grid": "#c6c6c6",
    "Grid_Fill": "#c6c6c6",
    "Display_Background": "#ffffff",
    "On": "#24a148",
    "Off": "#0e6027",
    "Header_Background": "#393939",
    "Header_ForeGround": "#ffffff",
    "Attention": "#ff832b",
    "STOP": "#da1e28",
}

# Dark values redesigned to pass contrast, and the new Grid_Fill role.
DARK = {
    "Grid": "#757575",
    "Button_Background": "#787878",
    "MAJOR": "#ff8389",
    "STOP": "#ff8389",
}
GRID_FILL = {"light": "#c6c6c6", "dark": "#2e2e2e"}

CANONICAL_HEX = re.compile(r"^#[0-9a-f]{6}([0-9a-f]{2})?$")


@pytest.fixture(scope="module")
def raw() -> str:
    return SCHEME_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scheme(raw) -> dict:
    return json.loads(raw)


def _sibling_roots() -> list[Path]:
    value = os.environ.get(SIBLINGS_ENV, "").strip()
    if not value:
        pytest.skip(f"{SIBLINGS_ENV} not set; sibling schema checkouts unavailable")
    return [Path(part) for part in value.split(os.pathsep) if part.strip()]


def test_scheme_is_schema_valid(scheme):
    roots = _sibling_roots()
    candidates = [root / SCHEMA_RELATIVE for root in roots if (root / SCHEMA_RELATIVE).is_file()]
    assert candidates, f"{SCHEMA_RELATIVE} not found under any root in {SIBLINGS_ENV}={roots}"
    schema = json.loads(candidates[0].read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(scheme), key=str)
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def test_scheme_is_root(scheme):
    assert scheme["id"] == "canopy-default"
    assert "extends" not in scheme


def test_roles_are_exactly_the_enumerated_set(scheme):
    assert list(scheme["roles"]) == ROLES


@pytest.mark.parametrize("role", ROLES)
def test_both_modes_are_canonical_hex(scheme, role):
    value = scheme["roles"][role]
    assert set(value) == {"light", "dark"}, role
    for mode in ("light", "dark"):
        color = value[mode]
        assert CANONICAL_HEX.match(color), f"{role}.{mode}={color!r} is not canonical hex"
        assert not (len(color) == 9 and color.endswith("ff")), f"{role}.{mode} has opaque alpha"


def test_display_background_values(scheme):
    assert scheme["roles"]["Display_Background"] == {"light": "#ffffff", "dark": "#161616"}


@pytest.mark.parametrize("role", sorted(ALARM_LIGHT))
def test_alarm_light_values_match_runtime_tokens(scheme, role):
    assert scheme["roles"][role]["light"] == ALARM_LIGHT[role]


@pytest.mark.parametrize("role", ROLES)
def test_light_values_unchanged(scheme, role):
    assert scheme["roles"][role]["light"] == LIGHT[role]


@pytest.mark.parametrize("role", sorted(DARK))
def test_redesigned_dark_values(scheme, role):
    assert scheme["roles"][role]["dark"] == DARK[role]


def test_grid_fill_values(scheme):
    assert scheme["roles"]["Grid_Fill"] == GRID_FILL


def test_grid_fill_light_matches_grid_light(scheme):
    assert scheme["roles"]["Grid_Fill"]["light"] == scheme["roles"]["Grid"]["light"]


def test_phoebus_import_maps_standard_names(scheme):
    phoebus = scheme["import"]["phoebus"]
    expected = {name: name for name in PHOEBUS_NAMES}
    expected["Display_Background"] = "Display_Background"
    expected["Grid"] = GRID_IMPORT
    assert phoebus == expected


def test_phoebus_import_splits_grid_background(scheme):
    assert scheme["import"]["phoebus"]["Grid"] == GRID_IMPORT


def test_phoebus_import_maps_display_background_to_itself(scheme):
    assert scheme["import"]["phoebus"]["Display_Background"] == "Display_Background"


def test_edm_import_table(scheme):
    assert scheme["import"]["edm"] == EDM_IMPORT


def test_edm_import_names_exist_in_stock_palette():
    colors_list = Path(canopy.converter.__file__).parent / "data" / "colors.list"
    names = set(re.findall(r'^static\s+\d+\s+"([^"]+)"', colors_list.read_text(), re.MULTILINE))
    missing = sorted(set(EDM_IMPORT) - names)
    assert not missing, f"not stock colors.list names: {missing}"


@pytest.mark.parametrize("source", ["phoebus", "edm"])
def test_every_import_target_is_a_role(scheme, source):
    bad = {}
    for name, target in scheme["import"][source].items():
        if isinstance(target, dict):
            # Slot object: a default "role" plus per-slot roles, every one a role.
            if "role" not in target or any(v not in ROLES for v in target.values()):
                bad[name] = target
        elif target not in ROLES:
            bad[name] = target
    assert not bad, f"import.{source} targets that are not roles: {bad}"


def test_single_final_newline(raw):
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")


def test_bytes_are_canonical(raw, scheme):
    assert raw == json.dumps(scheme, indent=2) + "\n"
