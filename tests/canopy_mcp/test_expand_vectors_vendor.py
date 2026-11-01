"""canopy's copy of the expand() vectors must stay byte-identical to their source.

``tests/canopy_mcp/fixtures/expand-vectors.json`` is the file
``canopy_mcp.screens`` is tested against. canopy-screen-runtime generates the
original at ``packages/runtime/src/render/__fixtures__/expand-vectors.json``
(from its expand cases); canopy-sdk-js keeps a copy at
``packages/screen-ir/test-vectors/expand.json``. A silent drift would let
canopy's walk pass against vectors the runtime no longer produces, so this test
compares the raw bytes with every copy it finds.

The sibling checkouts only exist on a developer machine, so the test reads their
roots from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list) and is skipped when the variable is unset.
"""

import os
from pathlib import Path

import pytest

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"
SOURCES = (
    "packages/runtime/src/render/__fixtures__/expand-vectors.json",
    "packages/screen-ir/test-vectors/expand.json",
)
CANOPY_COPY = Path(__file__).resolve().parent / "fixtures" / "expand-vectors.json"


def _sibling_roots() -> list[Path]:
    raw = os.environ.get(SIBLINGS_ENV, "").strip()
    if not raw:
        pytest.skip(f"{SIBLINGS_ENV} not set; sibling checkouts unavailable")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def test_expand_vectors_match_their_source():
    roots = _sibling_roots()
    candidates = [root / rel for root in roots for rel in SOURCES if (root / rel).is_file()]
    assert candidates, f"none of {SOURCES} found under any root in {SIBLINGS_ENV}={roots}"
    expected = CANOPY_COPY.read_bytes()
    for path in candidates:
        assert path.read_bytes() == expected, f"canopy's expand vectors differ from {path}"
