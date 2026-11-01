"""The SDK's copy of the write-outcome vectors must stay byte-identical to canopy's file.

``canopy/contracts/write_outcome_vectors.json`` is the table a client maps write outcomes
from; canopy-sdk-js vendors a byte copy at
``packages/client/test-vectors/write_outcome_vectors.json`` and tests its own write-outcome
code against that copy. A copy that drifts lets the SDK pass against a rule this
file no longer states, so this test compares the raw bytes.

The sibling checkout only exists on a developer machine, so the test reads its root
from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list) and is skipped when
the variable is unset.
"""

import os
from pathlib import Path
from importlib import resources

import pytest

from canopy.contracts import WRITE_OUTCOME_VECTORS_FILE

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"
SDK_COPY = "packages/client/test-vectors/write_outcome_vectors.json"


def _sibling_roots() -> list[Path]:
    raw = os.environ.get(SIBLINGS_ENV, "").strip()
    if not raw:
        pytest.skip(f"{SIBLINGS_ENV} not set; sibling sdk checkout unavailable")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def test_write_vectors_match_the_sdk():
    roots = _sibling_roots()
    candidates = [root / SDK_COPY for root in roots if (root / SDK_COPY).is_file()]
    assert candidates, f"{SDK_COPY} not found under any root in {SIBLINGS_ENV}={roots}"
    expected = (resources.files("canopy.contracts") / WRITE_OUTCOME_VECTORS_FILE).read_bytes()
    for path in candidates:
        assert path.read_bytes() == expected, f"the SDK's write vectors differ from canopy's: {path}"
