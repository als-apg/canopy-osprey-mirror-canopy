"""Snapshot listing bounds and paging at the service layer — no database involved."""

import base64
from datetime import UTC, datetime, timezone, timedelta

import pytest

from canopy.core.errors import BadRequest
from canopy.squirrel.services.snapshot_service import (
    SnapshotService,
    _decode_cursor,
    _encode_cursor,
)


class _RecordingSnapshotRepo:
    """Stands in for ``SnapshotRepository``: records ``search`` calls and returns an empty page."""

    def __init__(self) -> None:
        self.searches: list[dict] = []

    async def search(self, **kwargs) -> tuple[list, None, int]:
        self.searches.append(kwargs)
        return [], None, 0

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


async def test_paging_defaults_reach_the_repository(service, repo):
    await service.list_snapshots()

    assert repo.searches[0]["limit"] == 100
    assert repo.searches[0]["after"] is None


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


@pytest.mark.parametrize(
    ("bound", "value"),
    [
        ("start", datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5)))),
        ("end", datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5)))),
    ],
)
async def test_bound_overflowing_on_utc_conversion_is_a_bad_request(service, repo, bound, value):
    with pytest.raises(BadRequest, match=f"{bound} .* out of range"):
        await service.list_snapshots(**{bound: value})

    assert repo.searches == []


_SNAPSHOT_ID = "0bd19382-66ec-4294-a604-cb04c7fa4ad5"


def test_cursor_token_round_trips():
    cursor = (datetime(2026, 7, 20, 6, 17, 24, 123456, tzinfo=UTC), _SNAPSHOT_ID)

    assert _decode_cursor(_encode_cursor(cursor)) == cursor


def test_cursor_token_is_utc_whatever_the_input_zone():
    local = datetime(2026, 1, 1, 5, 0, tzinfo=timezone(timedelta(hours=-7)))

    created_at, _ = _decode_cursor(_encode_cursor((local, _SNAPSHOT_ID)))

    assert created_at == local
    assert created_at.utcoffset() == timedelta(0)


def test_no_cursor_means_no_token():
    assert _encode_cursor(None) is None
    assert _decode_cursor(None) is None


async def test_listing_decodes_the_token_for_the_repository(service, repo):
    cursor = (datetime(2026, 7, 20, tzinfo=UTC), _SNAPSHOT_ID)

    page = await service.list_snapshots(page_size=25, continuation_token=_encode_cursor(cursor))

    assert repo.searches[0]["limit"] == 25
    assert repo.searches[0]["after"] == cursor
    assert page.continuation_token is None
    assert page.total_count == 0


def _token(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "token",
    [
        "not base64!",
        _token("no-separator"),
        _token("2026-01-01T00:00:00+00:00|not-a-uuid"),
        _token(f"not-a-time|{_SNAPSHOT_ID}"),
        _token(f"2026-01-01T00:00:00|{_SNAPSHOT_ID}"),  # naive time
        _token(f"2026-01-01T00:00:00+00:00|{_SNAPSHOT_ID}|extra"),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),  # not UTF-8
    ],
)
async def test_malformed_token_is_a_bad_request(service, repo, token):
    with pytest.raises(BadRequest, match="Invalid continuationToken"):
        await service.list_snapshots(continuation_token=token)

    assert repo.searches == []
