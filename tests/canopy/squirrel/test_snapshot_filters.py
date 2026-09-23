"""Snapshot listing datetime bounds at the service layer — no database involved."""

from datetime import UTC, datetime, timezone, timedelta

import pytest

from canopy.core.errors import BadRequest
from canopy.squirrel.services.snapshot_service import SnapshotService


class _RecordingSnapshotRepo:
    """Stands in for ``SnapshotRepository``: records ``search`` calls and returns no rows."""

    def __init__(self) -> None:
        self.searches: list[dict] = []

    async def search(self, **kwargs) -> list:
        self.searches.append(kwargs)
        return []

    async def get_value_counts_batch(self, snapshot_ids: list[str]) -> dict[str, int]:
        return {}


class _NoMetaPvs:
    """Stands in for ``MetaPvRepository`` with no active meta PVs."""

    async def get(self, active: bool) -> list:
        return []


@pytest.fixture
def repo() -> _RecordingSnapshotRepo:
    return _RecordingSnapshotRepo()


@pytest.fixture
def service(repo) -> SnapshotService:
    """A service whose repositories never touch a session."""
    svc = SnapshotService(session=None)
    svc.snapshot_repo = repo
    svc.meta_pv_repo = _NoMetaPvs()
    return svc


async def test_bounds_reach_the_repository_as_aware_utc(service, repo):
    await service.list_snapshots(
        start=datetime(2026, 1, 1, 12, 0),
        end=datetime(2026, 1, 2, 5, 0, tzinfo=timezone(timedelta(hours=-7))),
    )

    assert repo.searches[0]["start"] == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert repo.searches[0]["end"] == datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


async def test_omitted_bounds_stay_none(service, repo):
    await service.list_snapshots()

    assert repo.searches[0]["start"] is None
    assert repo.searches[0]["end"] is None


async def test_start_after_end_is_a_bad_request(service, repo):
    with pytest.raises(BadRequest, match="must not be after end"):
        await service.list_snapshots(
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 5, 1, tzinfo=UTC),
        )

    assert repo.searches == []


async def test_start_after_end_is_compared_as_instants(service, repo):
    await service.list_snapshots(
        start=datetime(2026, 1, 1, 18, 0, tzinfo=UTC),
        end=datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7))),
    )

    assert len(repo.searches) == 1


async def test_equal_bounds_are_allowed(service, repo):
    instant = datetime(2026, 1, 1, tzinfo=UTC)

    await service.list_snapshots(start=instant, end=instant)

    assert repo.searches[0]["start"] == repo.searches[0]["end"] == instant
