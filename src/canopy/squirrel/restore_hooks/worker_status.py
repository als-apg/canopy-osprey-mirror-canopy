"""The worker's own view of its restore hooks, handed to the API through Redis.

Restores run in ``canopy-worker``, so the hooks that matter are the ones the worker
resolved from its environment. The worker publishes ``describe_restore_hooks()`` at
startup and the API's status endpoint reads it back. That makes a
``CANOPY_SQUIRREL__RESTORE_HOOKS`` that reached the API container but not the worker
visible, instead of reporting the API's view as if it were the worker's.

The report has no expiry: it is replaced on every worker start and carries the time
and host it was written from, so a stale one is recognisable rather than vanishing.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from datetime import UTC, datetime

from redis.asyncio import Redis

from canopy.squirrel.restore_hooks.registry import describe_restore_hooks

WORKER_STATUS_KEY = "canopy:squirrel:restore_hooks:worker"


async def publish_worker_status(redis: Redis) -> dict[str, Any]:
    """Record this process's restore-hook resolution as the worker's report.

    Parameters
    ----------
    redis : Redis
        The worker's Redis connection (Arq's ``ctx["redis"]``).

    Returns
    -------
    dict[str, Any]
        The report written: ``describe_restore_hooks()`` plus ``reported_at`` (ISO-8601,
        UTC) and ``host``.
    """
    report = {
        **describe_restore_hooks(),
        "reported_at": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
    }
    await redis.set(WORKER_STATUS_KEY, json.dumps(report))
    return report


async def read_worker_status(redis: Redis) -> dict[str, Any] | None:
    """Read the report the worker last published.

    Parameters
    ----------
    redis : Redis
        Any connection to the Redis the worker uses.

    Returns
    -------
    dict[str, Any] | None
        The report as published, or ``None`` when no worker has published one.
    """
    raw = await redis.get(WORKER_STATUS_KEY)
    return None if raw is None else json.loads(raw)
