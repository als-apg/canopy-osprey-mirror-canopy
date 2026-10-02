"""The PyDM converter's vendored widget registry must equal canopy's, byte for byte.

``widget-registry-data/widgets/*.json`` is the source of truth; the PyDM converter
vendors a copy under ``pydmconverter/ir/data/widget-registry``. The converter also
keeps a ``.beaver-snapshot-sha`` marker there that canopy has no counterpart for,
so only the ``*.json`` files are compared.

The sibling checkouts only exist on a developer machine, so the test reads their
roots from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list). The
validation gate sets the variable; an unset variable fails the test rather than
skipping it, so the comparison cannot be silently dropped.
"""

import os
from pathlib import Path

import pytest

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"
PYDM_REGISTRY_RELATIVE = "pydmconverter/ir/data/widget-registry"
CANOPY_REGISTRY = Path(__file__).resolve().parents[3] / "widget-registry-data" / "widgets"


def _sibling_roots() -> list[Path]:
    raw = os.environ.get(SIBLINGS_ENV, "").strip()
    assert raw, f"{SIBLINGS_ENV} must be set to the sibling checkouts (os.pathsep-separated)"
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def _pydm_registries() -> list[Path]:
    roots = _sibling_roots()
    found = [root / PYDM_REGISTRY_RELATIVE for root in roots if (root / PYDM_REGISTRY_RELATIVE).is_dir()]
    assert found, f"{PYDM_REGISTRY_RELATIVE} not found under any root in {SIBLINGS_ENV}={roots}"
    return found


def _json_files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.glob("*.json"))}


def test_canopy_registry_is_not_empty():
    assert _json_files(CANOPY_REGISTRY), f"no registry files under {CANOPY_REGISTRY}"


def test_pydm_vendored_registry_has_the_same_files():
    expected = set(_json_files(CANOPY_REGISTRY))
    for registry in _pydm_registries():
        assert set(_json_files(registry)) == expected, f"file set differs in {registry}"


@pytest.mark.parametrize("name", sorted(_json_files(CANOPY_REGISTRY)))
def test_pydm_vendored_registry_file_matches_byte_for_byte(name):
    expected = (CANOPY_REGISTRY / name).read_bytes()
    for registry in _pydm_registries():
        assert (registry / name).read_bytes() == expected, f"{name} differs in {registry}"
