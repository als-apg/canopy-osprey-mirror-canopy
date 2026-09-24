"""Fixtures for the canopy_mcp tests.

The whole directory needs the ``mcp`` extra; without it every test here is
skipped rather than failing on import. Tests marked ``live`` need a running
canopy stack and only run when ``CANOPY_LIVE=1``.
"""

import os

import pytest

pytest.importorskip("fastmcp")

LIVE_ENV = "CANOPY_LIVE"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV, "").strip() == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 with a running canopy stack")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
