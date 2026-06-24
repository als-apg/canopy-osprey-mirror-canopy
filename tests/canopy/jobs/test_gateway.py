"""Unit tests for jobs.gateway.get_status — the Arq-state → JobStatusDTO mapping.

Arq's `Job` and the Redis pool are faked, so these need no running Redis.
"""

import json
from datetime import datetime, UTC

import pytest
from arq.jobs import JobStatus

from canopy.jobs import gateway
from canopy.jobs.errors import JobAlreadyExists


class FakeInfo:
    def __init__(
        self, function="task", enqueue_time=None, success=None, result=None, start_time=None, finish_time=None
    ):
        self.function = function
        self.enqueue_time = enqueue_time
        self.success = success
        self.result = result
        self.start_time = start_time
        self.finish_time = finish_time


class FakeJob:
    def __init__(self, status, info=None, result_info=None):
        self._status, self._info, self._result_info = status, info, result_info

    async def status(self):
        return self._status

    async def info(self):
        return self._info

    async def result_info(self):
        return self._result_info


class FakePool:
    def __init__(self, progress=None):
        self._progress = progress

    async def get(self, key):
        return json.dumps(self._progress) if self._progress is not None else None


@pytest.fixture
def patch_gateway(monkeypatch):
    def _apply(status, info=None, result_info=None, progress=None):
        async def fake_get_pool():
            return FakePool(progress)

        monkeypatch.setattr(gateway, "get_pool", fake_get_pool)
        monkeypatch.setattr(gateway, "Job", lambda job_id, pool: FakeJob(status, info, result_info))

    return _apply


async def test_not_found_returns_none(patch_gateway):
    patch_gateway(JobStatus.not_found)
    assert await gateway.get_status("x") is None


async def test_queued_is_pending(patch_gateway):
    t = datetime(2026, 1, 1, tzinfo=UTC)
    patch_gateway(JobStatus.queued, info=FakeInfo(function="create_snapshot", enqueue_time=t))
    dto = await gateway.get_status("x")
    assert dto.status == "pending"
    assert dto.function == "create_snapshot"
    assert dto.enqueued_at == t
    assert dto.progress == 0


async def test_in_progress_reads_redis_progress(patch_gateway):
    patch_gateway(JobStatus.in_progress, info=FakeInfo(), progress={"percent": 42, "message": "halfway"})
    dto = await gateway.get_status("x")
    assert dto.status == "running"
    assert dto.progress == 42
    assert dto.message == "halfway"


async def test_complete_success_carries_opaque_result(patch_gateway):
    ri = FakeInfo(success=True, result={"type": "snapshot", "id": "s1"})
    patch_gateway(JobStatus.complete, info=ri, result_info=ri)
    dto = await gateway.get_status("x")
    assert dto.status == "completed"
    assert dto.progress == 100
    assert dto.result == {"type": "snapshot", "id": "s1"}
    assert dto.error is None


async def test_complete_failure_surfaces_error(patch_gateway):
    ri = FakeInfo(success=False, result=ValueError("boom"))
    patch_gateway(JobStatus.complete, info=ri, result_info=ri)
    dto = await gateway.get_status("x")
    assert dto.status == "failed"
    assert dto.result is None
    assert "boom" in dto.error


async def test_complete_but_result_expired_is_completed_not_failed(patch_gateway):
    # status() saw the result key, but result_info() is None because its
    # keep_result TTL expired in between. The job succeeded — it must not be
    # reported as failed.
    patch_gateway(JobStatus.complete, result_info=None)
    dto = await gateway.get_status("x")
    assert dto.status == "completed"
    assert dto.progress == 100
    assert dto.error is None
    assert dto.result is None


async def test_enqueue_duplicate_raises_job_already_exists(monkeypatch):
    class DupPool:
        async def enqueue_job(self, function, *args, **kwargs):
            return None  # Arq returns None when a job with the same id exists

    async def fake_get_pool():
        return DupPool()

    monkeypatch.setattr(gateway, "get_pool", fake_get_pool)
    with pytest.raises(JobAlreadyExists):
        await gateway.enqueue("create_snapshot")
