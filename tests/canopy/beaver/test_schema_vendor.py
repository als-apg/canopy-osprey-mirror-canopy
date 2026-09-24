"""The vendored Screen IR schema must stay byte-identical to its sibling copies.

canopy serves ``screen-ir-1.0.schema.json`` from ``canopy.beaver.data``, but the
schema is owned by the SDK (``packages/screen-ir/schema/screen-ir.schema.json``)
and also vendored by the PyDM converter (``pydmconverter/ir/data``). A silent
drift between the copies means canopy advertises a contract the producers and
consumers do not share, so this test compares the raw bytes.

The sibling checkouts only exist on a developer machine, so the test reads their
roots from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list) and is
skipped when the variable is unset.
"""

import os
from pathlib import Path
from importlib.resources import files

import pytest

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"

SIBLING_COPIES = [
    "packages/screen-ir/schema/screen-ir.schema.json",
    "pydmconverter/ir/data/screen-ir.schema.json",
]


def _sibling_roots() -> list[Path]:
    raw = os.environ.get(SIBLINGS_ENV, "").strip()
    if not raw:
        pytest.skip(f"{SIBLINGS_ENV} not set; sibling schema checkouts unavailable")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def _canopy_schema_bytes() -> bytes:
    return files("canopy.beaver.data").joinpath("schemas/screen-ir-1.0.schema.json").read_bytes()


@pytest.mark.parametrize("relative", SIBLING_COPIES)
def test_vendored_schema_matches_sibling(relative):
    roots = _sibling_roots()
    candidates = [root / relative for root in roots if (root / relative).is_file()]
    assert candidates, f"{relative} not found under any root in {SIBLINGS_ENV}={roots}"
    expected = _canopy_schema_bytes()
    for path in candidates:
        assert path.read_bytes() == expected, f"canopy's vendored schema differs from {path}"
