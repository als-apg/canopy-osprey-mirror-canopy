"""Fixtures for the canopy_mcp tests.

The whole directory needs the ``mcp`` extra and a POSIX system (the draft
store takes ``fcntl`` file locks); without either every test here is skipped
rather than failing on import. Tests marked ``live`` need a running canopy
stack and only run when ``CANOPY_LIVE=1``.

The FastMCP instance is one per process, so an app built with authoring off
takes the authoring tools away from every later test. ``restore_authoring``
puts them back after each test.

In hare mode DATA's summary (read by ``config.json`` and ``/health/summary``)
comes through ``static.SummaryReader.read``. A test app's shared client points at
the default backend URL, where a developer's own stack may be listening, so
``no_data_summary`` stubs ``SummaryReader.read`` to "no DATA" for every test. A
test marked ``real_data_summary`` keeps the real reader and supplies its own
``app.state.http``; a summary body is built with ``summary()``.

Starlette's test client names its server ``testserver``, a host the app's
``HostGuard`` refuses, so ``loopback_test_client`` makes ``http://127.0.0.1``
the default base URL of every test client; a test that passes ``base_url``
keeps its own.

``CANOPY_SPEC_DIR`` is removed for every test, so a developer's staged spec
never decides a result; the spec-present tests stage their own (``synthetic_spec``).
"""

import os
from typing import Any
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

pytest.importorskip("fastmcp")
pytest.importorskip("fcntl", reason="the panel server is POSIX-only: its draft store takes fcntl file locks")

from canopy_mcp import static  # noqa: E402
from canopy_mcp.config import get_settings  # noqa: E402
from canopy_mcp.server import (  # noqa: E402
    mcp,
    register_all,
    registered_tools,
    authoring_snapshot,
)
from canopy.contracts.health import HareHealthSummaryDTO  # noqa: E402

LIVE_ENV = "CANOPY_LIVE"
LOOPBACK_BASE_URL = "http://127.0.0.1"
REAL_DATA_SUMMARY = "real_data_summary"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{REAL_DATA_SUMMARY}: keep the real SummaryReader.read for DATA's summary (read by config.json and /health/summary)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV, "").strip() == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 with a running canopy stack")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def summary(**overrides: Any) -> dict[str, Any]:
    """DATA's summary body in wire form, built from the contract model; ``overrides`` are model field names."""
    defaults: dict[str, Any] = {
        "status": "ok",
        "backend": "sim",
        "connector_ok": True,
        "clients": 1,
        "active_monitors": 2,
        "connected": 2,
        "subscriptions": 2,
        "label": "Simulator",
        "notice": None,
        "binding": "va@3",
    }
    return HareHealthSummaryDTO(**{**defaults, **overrides}).model_dump(by_alias=True, mode="json")


@pytest.fixture(autouse=True)
def restore_authoring() -> Iterator[None]:
    """Start every test from the tool set a default app offers: every tool.

    Any authoring tool a test's app removed is re-added afterwards, so no test
    inherits another test's app.
    """
    register_all()
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
    """``SummaryReader.read`` stubbed to no DATA's summary (read by ``config.json`` and ``/health/summary``) unless the test is marked ``real_data_summary``."""
    if request.node.get_closest_marker(REAL_DATA_SUMMARY) is not None:
        return

    async def no_data(self: object, app: object) -> None:
        return None

    monkeypatch.setattr(static.SummaryReader, "read", no_data)


@pytest.fixture(autouse=True)
def no_ambient_spec(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No staged spec reaches a test from the environment.

    The cached process settings are dropped around each test, so a test that
    stages a spec (or a viewer dist) through the environment is the one the
    tools read.
    """
    monkeypatch.delenv("CANOPY_SPEC_DIR", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def loopback_test_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``TestClient`` built without a ``base_url`` addresses ``http://127.0.0.1``."""
    original = TestClient.__init__

    def init(self: TestClient, app: Any, *args: Any, **kwargs: Any) -> None:
        if not args:
            kwargs.setdefault("base_url", LOOPBACK_BASE_URL)
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
