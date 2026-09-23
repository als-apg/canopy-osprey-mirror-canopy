"""The worker's restore-hook report: published at worker startup, read back from Redis."""

import json
import socket
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


def _configure_hooks(monkeypatch, restore_hooks: str) -> None:
    monkeypatch.setattr(registry, "get_squirrel_config", lambda: SquirrelConfig(restore_hooks=restore_hooks))


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


async def test_worker_startup_publishes_its_hooks(monkeypatch):
    _configure_hooks(monkeypatch, "write_to_con")
    redis = fakeredis.aioredis.FakeRedis()
    ctx = {"redis": redis}

    await worker.on_startup(ctx)
    try:
        report = json.loads(await redis.get(WORKER_STATUS_KEY))
        assert report["loaded"] == ["write_to_con"]
    finally:
        await ctx["epics"].close()
