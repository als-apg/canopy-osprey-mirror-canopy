"""Fixtures for the canopy_mcp tests.

The whole directory needs the ``mcp`` extra; without it every test here is
skipped rather than failing on import. Tests marked ``live`` need a running
canopy stack and only run when ``CANOPY_LIVE=1``.

The FastMCP instance is one per process, so an app built with authoring off
takes the six authoring tools away from every later test. ``restore_authoring``
puts them back after each test.
"""

import os
from collections.abc import Iterator

import pytest

pytest.importorskip("fastmcp")

from canopy_mcp.server import mcp, registered_tools, authoring_snapshot  # noqa: E402

LIVE_ENV = "CANOPY_LIVE"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV, "").strip() == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 with a running canopy stack")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def restore_authoring() -> Iterator[None]:
    """Re-add any authoring tool a test's app removed, so the next test sees all fifteen."""
    yield
    removed = authoring_snapshot()
    if not removed:
        return
    present = registered_tools()
    for name, tool in removed.items():
        if name not in present:
            mcp.add_tool(tool)
