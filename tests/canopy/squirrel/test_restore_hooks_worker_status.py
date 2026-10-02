"""The worker's restore-hook report: published at worker startup, read back from Redis."""

import json
import socket
import logging
from datetime import UTC, datetime

import fakeredis.aioredis

from canopy import worker
from canopy.squirrel.config import SquirrelConfig
from canopy.squirrel.restore_hooks import (
    registry,
    read_worker_status,
    publish_worker_status,
)
from canopy.squirrel.restore_hooks.worker_status import WORKER_STATUS_KEY


def _configure_hooks(monkeypatch, restore_hooks: str, legacy_restore_hook: str = "") -> None:
    monkeypatch.setattr(
        registry,
        "get_squirrel_config",
        lambda: SquirrelConfig(restore_hooks=restore_hooks, restore_hook=legacy_restore_hook),
    )


async def test_read_is_none_before_any_worker_publishes():
    assert await read_worker_status(fakeredis.aioredis.FakeRedis()) is None


async def test_published_report_round_trips(monkeypatch):
    _configure_hooks(monkeypatch, "magnet_trim,nope")
    redis = fakeredis.aioredis.FakeRedis()

    published = await publish_worker_status(redis)

    assert await read_worker_status(redis) == published
    assert published["status"] == "partial"
    assert published["loaded"] == ["magnet_trim"]
    assert published["unknown"] == ["nope"]
    assert published["host"] == socket.gethostname()
    assert datetime.fromisoformat(published["reported_at"]).tzinfo == UTC


async def test_a_new_report_replaces_the_old_one(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis()
    _configure_hooks(monkeypatch, "magnet_trim")
    await publish_worker_status(redis)
    _configure_hooks(monkeypatch, "")
    await publish_worker_status(redis)

    assert (await read_worker_status(redis))["status"] == "disabled"


async def _start_worker(monkeypatch) -> dict:
    """Run ``worker.on_startup`` against fakeredis, with the hook registry's cache isolated."""
    monkeypatch.setattr(registry, "_hooks", [])
    monkeypatch.setattr(registry, "_hook_providers", None)
    ctx = {"redis": fakeredis.aioredis.FakeRedis()}
    await worker.on_startup(ctx)
    return ctx


async def test_worker_startup_publishes_its_hooks(monkeypatch):
    _configure_hooks(monkeypatch, "write_to_con")

    ctx = await _start_worker(monkeypatch)
    try:
        report = json.loads(await ctx["redis"].get(WORKER_STATUS_KEY))
        assert report["loaded"] == ["write_to_con"]
    finally:
        await ctx["epics"].close()


async def test_worker_startup_logs_hook_configuration_warnings(monkeypatch, caplog):
    _configure_hooks(monkeypatch, "write_to_con,nope", legacy_restore_hook="magnet_trim")

    with caplog.at_level(logging.WARNING, logger=registry.logger.name):
        ctx = await _start_worker(monkeypatch)
    try:
        assert registry.LEGACY_SETTING_WARNING in caplog.messages
        assert any("'nope'" in message for message in caplog.messages)
    finally:
        await ctx["epics"].close()
