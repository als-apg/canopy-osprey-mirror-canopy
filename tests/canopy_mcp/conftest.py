"""Fixtures for the canopy_mcp tests.

The whole directory needs the ``mcp`` extra; without it every test here is
skipped rather than failing on import. Tests marked ``live`` need a running
canopy stack and only run when ``CANOPY_LIVE=1``.

The FastMCP instance is one per process, so an app built with authoring off
takes the seven authoring tools away from every later test. ``restore_authoring``
puts them back after each test.

In hare mode ``GET /config.json`` asks DATA for its health summary. A test
app's shared client points at the default backend URL, where a developer's own
stack may be listening, so ``no_data_summary`` stubs that fetch to "no DATA"
for every test. A test marked ``real_data_summary`` keeps the real fetch and
supplies its own ``app.state.http``.
"""

import os
from collections.abc import Iterator

import pytest

pytest.importorskip("fastmcp")

from canopy_mcp import static  # noqa: E402
from canopy_mcp.server import mcp, registered_tools, authoring_snapshot  # noqa: E402

LIVE_ENV = "CANOPY_LIVE"
REAL_DATA_SUMMARY = "real_data_summary"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"{REAL_DATA_SUMMARY}: keep config.json's real fetch of DATA's summary")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV, "").strip() == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 with a running canopy stack")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def restore_authoring() -> Iterator[None]:
    """Re-add any authoring tool a test's app removed, so the next test sees every tool."""
    yield
    removed = authoring_snapshot()
    if not removed:
        return
    present = registered_tools()
    for name, tool in removed.items():
        if name not in present:
            mcp.add_tool(tool)


@pytest.fixture(autouse=True)
def no_data_summary(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """``config.json`` sees no DATA (both identity fields null) unless the test is marked ``real_data_summary``."""
    if request.node.get_closest_marker(REAL_DATA_SUMMARY) is not None:
        return

    async def no_data(request: object) -> None:
        return None

    monkeypatch.setattr(static, "fetch_data_summary", no_data)
