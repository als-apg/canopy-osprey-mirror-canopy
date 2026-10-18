"""The SDK's copy of the heartbeat vectors must stay byte-identical to canopy's file.

``canopy/contracts/heartbeat_vectors.json`` states the machine statement a heartbeat
carries, the writes cause table and the badge and toasts the viewer shows for each
frame sequence; the SDK vendors a byte copy at
``packages/client/test-vectors/heartbeat_vectors.json`` and tests its heartbeat
handling against that copy. A copy that drifts lets the SDK pass against a rule this
file no longer states, so this test compares the raw bytes, as
``test_write_vectors_vendor.py`` does for the WC6 vectors.

The sibling checkout only exists on a developer machine, so the test reads its root
from ``CANOPY_SCHEMA_SIBLINGS`` (an ``os.pathsep``-separated list) and is skipped when
the variable is unset.
"""

import os
from pathlib import Path
from importlib import resources

import pytest

from canopy.contracts import HEARTBEAT_VECTORS_FILE

SIBLINGS_ENV = "CANOPY_SCHEMA_SIBLINGS"
SDK_COPY = "packages/client/test-vectors/heartbeat_vectors.json"


def _sibling_roots() -> list[Path]:
    raw = os.environ.get(SIBLINGS_ENV, "").strip()
    if not raw:
        pytest.skip(f"{SIBLINGS_ENV} not set; sibling sdk checkout unavailable")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


def test_heartbeat_vectors_match_the_sdk():
    roots = _sibling_roots()
    candidates = [root / SDK_COPY for root in roots if (root / SDK_COPY).is_file()]
    assert candidates, f"{SDK_COPY} not found under any root in {SIBLINGS_ENV}={roots}"
    expected = (resources.files("canopy.contracts") / HEARTBEAT_VECTORS_FILE).read_bytes()
    for path in candidates:
        assert path.read_bytes() == expected, f"the SDK's heartbeat vectors differ from canopy's: {path}"
