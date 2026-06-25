"""Public surface of the Jobs module — a thin facade over the Arq queue.

Other modules enqueue long-running work via ``enqueue(...)``; the worker runs it
and clients poll ``GET /jobs/v1/{id}``. Job *results* are opaque references (e.g.
``{"type": "snapshot", "id": ...}``) so this module never imports squirrel/mole/etc.
Granular progress rides a per-job Redis key, since Arq's native status is only
queued / in_progress / complete.

The Redis pool connects lazily on first use, so mounting Jobs does not make Redis
a hard startup dependency of canopy-api.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any

from arq import create_pool
from arq.jobs import Job, JobStatus
from arq.connections import ArqRedis, RedisSettings

from canopy.core.config import get_settings
from canopy.jobs.errors import JobAlreadyExists
from canopy.jobs.schemas.job import JobStatusDTO

_pool: ArqRedis | None = None
_redis_url: str | None = None
_lock = asyncio.Lock()


def configure(redis_url: str) -> None:
    """Record the Redis URL at startup. Does not connect."""
    global _redis_url
    _redis_url = redis_url


async def get_pool() -> ArqRedis:
    """Return the Arq Redis pool, connecting lazily (and exactly once) on first use."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                url = _redis_url or get_settings().redis_url
                _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue(function: str, *args: Any, **kwargs: Any) -> str:
    """Enqueue a worker task and return its job id for polling."""
    pool = await get_pool()
    job = await pool.enqueue_job(function, *args, **kwargs)
    if job is None:
        raise JobAlreadyExists(f"Cannot enqueue {function!r}: a job with the same id already exists")
    return job.job_id


# --- progress: a per-job Redis key written from inside running tasks -------- #

_PROGRESS_PREFIX = "canopy:job_progress:"
_PROGRESS_TTL = 3600


async def report_progress(redis: ArqRedis, job_id: str, percent: int, message: str | None = None) -> None:
    """Publish coarse progress from inside a running task (Arq passes the pool as ctx['redis'])."""
    await redis.set(_PROGRESS_PREFIX + job_id, json.dumps({"percent": percent, "message": message}), ex=_PROGRESS_TTL)


async def _read_progress(redis: ArqRedis, job_id: str) -> tuple[int, str | None]:
    raw = await redis.get(_PROGRESS_PREFIX + job_id)
    if not raw:
        return 0, None
    data = json.loads(raw)
    return int(data.get("percent", 0)), data.get("message")


# --- status ---------------------------------------------------------------- #


async def get_status(job_id: str) -> JobStatusDTO | None:
    """Build a JobStatusDTO from Arq job state + the Redis progress key, or None if unknown."""
    pool = await get_pool()
    job = Job(job_id, pool)
    status = await job.status()
    if status == JobStatus.not_found:
        return None

    percent, message = await _read_progress(pool, job_id)

    if status == JobStatus.complete:
        # A completed job's metadata (function, timings) rides on its JobResult,
        # so result_info() alone covers this branch — no extra info() round-trip.
        result_info = await job.result_info()
        if result_info is None:
            # status() saw the result key, but its keep_result TTL expired before
            # this read. The job finished; report it completed rather than
            # inventing a failure from the now-missing result.
            return JobStatusDTO(id=job_id, status="completed", progress=100, message=message)
        success = bool(result_info.success)
        raw_result = result_info.result
        return JobStatusDTO(
            id=job_id,
            function=result_info.function,
            status="completed" if success else "failed",
            progress=100 if success else percent,
            message=message,
            result=raw_result if success else None,
            error=None if success else (str(raw_result) if raw_result is not None else "Job failed"),
            enqueued_at=result_info.enqueue_time,
            started_at=result_info.start_time,
            finished_at=result_info.finish_time,
        )

    # Not yet complete: no result exists, so read the queued JobDef for metadata.
    info = await job.info()
    return JobStatusDTO(
        id=job_id,
        function=getattr(info, "function", None),
        status="running" if status == JobStatus.in_progress else "pending",
        progress=percent,
        message=message,
        enqueued_at=getattr(info, "enqueue_time", None),
    )
