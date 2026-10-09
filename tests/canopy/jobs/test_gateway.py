"""Unit tests for jobs.gateway.get_status — the Arq-state → JobStatusDTO mapping.

Arq's `Job` and the Redis pool are faked, so these need no running Redis.
"""

import json
from datetime import UTC, datetime

import pytest
from arq.jobs import JobStatus
from pydantic import ValidationError

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


# --- phases ------------------------------------------------------------------ #

_PHASES = [
    {"name": "Setpoints", "current": 2, "total": 2, "status": "completed", "failures": 1},
    {"name": "MagnetTrimHook", "current": 0, "total": 0, "status": "skipped", "failures": 0},
]


class RecordingRedis:
    """Just enough of a Redis client for `report_progress` and `_read_progress`."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


async def test_progress_without_phases_reports_none(patch_gateway):
    patch_gateway(JobStatus.in_progress, info=FakeInfo(), progress={"percent": 42, "message": "halfway"})
    assert (await gateway.get_status("x")).phases == []


async def test_running_job_carries_its_phases(patch_gateway):
    patch_gateway(JobStatus.in_progress, info=FakeInfo(), progress={"percent": 50, "message": "m", "phases": _PHASES})
    dto = await gateway.get_status("x")
    assert [(p.name, p.status, p.current, p.total, p.failures) for p in dto.phases] == [
        ("Setpoints", "completed", 2, 2, 1),
        ("MagnetTrimHook", "skipped", 0, 0, 0),
    ]


async def test_completed_job_keeps_its_final_phases(patch_gateway):
    ri = FakeInfo(success=True, result={"type": "restore"})
    patch_gateway(JobStatus.complete, info=ri, result_info=ri, progress={"percent": 100, "phases": _PHASES})
    dto = await gateway.get_status("x")
    assert [p.name for p in dto.phases] == ["Setpoints", "MagnetTrimHook"]


async def test_expired_result_keeps_its_final_phases(patch_gateway):
    patch_gateway(JobStatus.complete, result_info=None, progress={"percent": 100, "phases": _PHASES})
    assert len((await gateway.get_status("x")).phases) == 2


async def test_phases_serialize_on_the_status_payload(patch_gateway):
    patch_gateway(JobStatus.in_progress, info=FakeInfo(), progress={"percent": 50, "phases": _PHASES[:1]})
    dumped = (await gateway.get_status("x")).model_dump(by_alias=True)
    assert dumped["phases"] == [_PHASES[0]]


async def test_report_progress_round_trips_phases():
    redis = RecordingRedis()
    await gateway.report_progress(redis, "j", 50, "msg", phases=_PHASES)
    percent, message, phases = await gateway._read_progress(redis, "j")
    assert (percent, message) == (50, "msg")
    assert [p.model_dump() for p in phases] == _PHASES


async def test_report_progress_without_phases_keeps_the_old_payload():
    redis = RecordingRedis()
    await gateway.report_progress(redis, "j", 10, "msg")
    assert json.loads(redis.store["canopy:job_progress:j"]) == {"percent": 10, "message": "msg"}


async def test_report_progress_fills_phase_defaults():
    redis = RecordingRedis()
    await gateway.report_progress(redis, "j", 0, phases=[{"name": "Setpoints", "total": 3}])
    stored = json.loads(redis.store["canopy:job_progress:j"])["phases"]
    assert stored == [{"name": "Setpoints", "current": 0, "total": 3, "status": "pending", "failures": 0}]


@pytest.mark.parametrize("phase", [{"name": "x", "status": "bogus"}, {"total": 3}, {"name": "x", "current": "lots"}])
async def test_report_progress_rejects_a_malformed_phase(phase):
    with pytest.raises(ValidationError):
        await gateway.report_progress(RecordingRedis(), "j", 0, phases=[phase])
