"""Tests for the squirrel health endpoints.

The restore-hooks status endpoint reads configuration only, so these run against the
sub-app directly instead of the DB-gated ``client`` fixture.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from canopy.squirrel.app import app as squirrel_app
from canopy.squirrel.config import SquirrelConfig
from canopy.squirrel.restore_hooks import registry

READ_HEADERS = {"X-API-Key": "test-read"}


@pytest_asyncio.fixture
async def http_client() -> AsyncClient:
    """An AsyncClient over the squirrel sub-app, with no database wired in."""
    async with AsyncClient(transport=ASGITransport(app=squirrel_app), base_url="http://test") as c:
        yield c


def _configure_hooks(monkeypatch, restore_hooks: str) -> None:
    monkeypatch.setattr(registry, "get_squirrel_config", lambda: SquirrelConfig(restore_hooks=restore_hooks))


async def test_restore_hooks_status_requires_a_key(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "")
    r = await http_client.get("/v1/health/restore-hooks")
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("configured", "status", "loaded", "unknown"),
    [
        ("", "disabled", [], []),
        ("magnet_trim,write_to_con", "active", ["magnet_trim", "write_to_con"], []),
        ("magnet_trim,nope", "partial", ["magnet_trim"], ["nope"]),
        ("nope", "unknown", [], ["nope"]),
    ],
)
async def test_restore_hooks_status(
    http_client: AsyncClient, monkeypatch, configured: str, status: str, loaded: list[str], unknown: list[str]
):
    _configure_hooks(monkeypatch, configured)
    r = await http_client.get("/v1/health/restore-hooks", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == status
    assert body["loaded"] == loaded
    assert body["unknown"] == unknown
    assert body["available"] == ["magnet_trim", "write_to_con"]


async def test_restore_hooks_status_builds_no_hook(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim")
    monkeypatch.setattr(registry, "_hooks", [])
    monkeypatch.setattr(registry, "_hook_providers", None)

    r = await http_client.get("/v1/health/restore-hooks", headers=READ_HEADERS)
    assert r.status_code == 200
    assert registry._hooks == []
    assert registry._hook_providers is None


async def test_watchdog_reports_the_module(http_client: AsyncClient):
    r = await http_client.get("/v1/health/watchdog")
    assert r.status_code == 200
    assert r.json()["module"] == "squirrel"
