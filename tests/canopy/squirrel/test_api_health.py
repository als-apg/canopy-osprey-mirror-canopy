"""Tests for the squirrel health endpoints.

The restore-hooks status endpoint reads configuration and the worker's Redis report
only, so these run against the sub-app directly with an in-memory Redis instead of
the DB-gated ``client`` fixture.
"""

import asyncio
from datetime import datetime

import pytest
import pytest_asyncio
import fakeredis.aioredis
from httpx import AsyncClient, ASGITransport
from redis.exceptions import ConnectionError as RedisConnectionError

from canopy.jobs import gateway as jobs_gateway
from canopy.squirrel.app import app as squirrel_app
from canopy.squirrel.api.v1 import health
from canopy.squirrel.config import SquirrelConfig
from canopy.squirrel.restore_hooks import registry, publish_worker_status

READ_HEADERS = {"X-API-Key": "test-read"}


@pytest.fixture
def redis(monkeypatch) -> fakeredis.aioredis.FakeRedis:
    """An in-memory Redis standing in for the Jobs pool the API reads the worker report from."""
    fake = fakeredis.aioredis.FakeRedis()

    async def get_pool():
        return fake

    monkeypatch.setattr(jobs_gateway, "get_pool", get_pool)
    return fake


@pytest_asyncio.fixture
async def http_client(redis) -> AsyncClient:
    """An AsyncClient over the squirrel sub-app, with no database wired in."""
    async with AsyncClient(transport=ASGITransport(app=squirrel_app), base_url="http://test") as c:
        yield c


def _configure_hooks(monkeypatch, restore_hooks: str, legacy_restore_hook: str = "") -> None:
    monkeypatch.setattr(
        registry,
        "get_squirrel_config",
        lambda: SquirrelConfig(restore_hooks=restore_hooks, restore_hook=legacy_restore_hook),
    )


async def _get_status(http_client: AsyncClient) -> dict:
    r = await http_client.get("/v1/health/restore-hooks", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


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
async def test_api_view(
    http_client: AsyncClient, monkeypatch, configured: str, status: str, loaded: list[str], unknown: list[str]
):
    _configure_hooks(monkeypatch, configured)
    body = await _get_status(http_client)
    assert body["api"]["status"] == status
    assert body["api"]["loaded"] == loaded
    assert body["api"]["unknown"] == unknown
    assert body["available"] == ["magnet_trim", "write_to_con"]


async def test_no_worker_report_yet(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim")
    body = await _get_status(http_client)
    assert body["worker"] is None
    assert body["matchesApi"] is None
    assert body["workerError"] is None


async def test_worker_report_matching_the_api(http_client: AsyncClient, redis, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim,write_to_con")
    await publish_worker_status(redis)

    body = await _get_status(http_client)
    assert body["worker"]["status"] == "active"
    assert body["worker"]["loaded"] == ["magnet_trim", "write_to_con"]
    assert body["worker"]["host"]
    assert datetime.fromisoformat(body["worker"]["reportedAt"]).tzinfo is not None
    assert body["matchesApi"] is True


async def test_worker_missing_the_variable_is_flagged(http_client: AsyncClient, redis, monkeypatch):
    _configure_hooks(monkeypatch, "")
    await publish_worker_status(redis)
    _configure_hooks(monkeypatch, "magnet_trim")

    body = await _get_status(http_client)
    assert body["api"]["status"] == "active"
    assert body["worker"]["status"] == "disabled"
    assert body["matchesApi"] is False


async def test_legacy_variable_warning_is_reported(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "", legacy_restore_hook="magnet_trim")
    body = await _get_status(http_client)
    assert body["api"]["status"] == "disabled"
    assert body["api"]["warnings"] == [registry.LEGACY_SETTING_WARNING]


async def test_unreachable_redis_is_reported_not_raised(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim")

    async def refuse():
        raise RedisConnectionError("Connection refused")

    monkeypatch.setattr(jobs_gateway, "get_pool", refuse)
    body = await _get_status(http_client)
    assert body["worker"] is None
    assert "Connection refused" in body["workerError"]
    assert body["api"]["status"] == "active"


async def test_slow_redis_times_out(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim")

    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(jobs_gateway, "get_pool", hang)
    monkeypatch.setattr(health, "WORKER_STATUS_TIMEOUT_S", 0.05)
    body = await _get_status(http_client)
    assert body["worker"] is None
    assert "TimeoutError" in body["workerError"]


async def test_restore_hooks_status_builds_no_hook(http_client: AsyncClient, monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim")
    monkeypatch.setattr(registry, "_hooks", [])
    monkeypatch.setattr(registry, "_hook_providers", None)

    await _get_status(http_client)
    assert registry._hooks == []
    assert registry._hook_providers is None


async def test_watchdog_reports_the_module(http_client: AsyncClient):
    r = await http_client.get("/v1/health/watchdog")
    assert r.status_code == 200
    assert r.json()["module"] == "squirrel"
