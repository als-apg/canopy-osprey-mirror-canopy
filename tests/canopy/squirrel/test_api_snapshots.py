"""API tests for the snapshot endpoints.

Create/restore enqueue Arq jobs (``jobs.gateway.enqueue`` is stubbed by the ``client``
fixture, which records calls in ``enqueued``). Read/compare/delete run inline, so those
snapshots are seeded directly through the shared test session.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.squirrel.data.models import Pv, Snapshot, SnapshotValue
from tests.canopy.squirrel.conftest import READ_HEADERS, WRITE_HEADERS


async def _seed_snapshot(
    session: AsyncSession,
    title: str,
    *,
    pv: Pv | None = None,
    value: float = 42.0,
    created_at: datetime | None = None,
) -> str:
    """Insert a snapshot, optionally with one PV value, and return its id.

    Parameters
    ----------
    session : AsyncSession
        Seeding session; the rows are committed so the separate request sessions see them.
    title : str
        Snapshot title.
    pv : Pv | None
        When given, one ``SnapshotValue`` for this PV is attached to the snapshot.
    value : float
        Setpoint value recorded for ``pv``.
    created_at : datetime | None
        Explicit creation timestamp; the database default is used when omitted.

    Returns
    -------
    str
        The id of the seeded snapshot.
    """
    snap = Snapshot(title=title, description="seeded")
    if created_at is not None:
        snap.created_at = created_at
    session.add(snap)
    await session.flush()
    if pv is not None:
        session.add(
            SnapshotValue(
                snapshot_id=snap.id,
                pv_id=pv.id,
                pv_name=pv.setpoint_address or pv.readback_address or "?",
                setpoint_value={"value": value, "severity": 0, "status": 0},
                status=0,
                severity=0,
                timestamp=datetime.now(UTC),
            )
        )
    await session.commit()
    return snap.id


async def _seed_pv(session: AsyncSession, setpoint: str = "CMP:PV:SP") -> Pv:
    pv = Pv(setpoint_address=setpoint, device="CMP")
    session.add(pv)
    await session.commit()
    return pv


async def test_list_snapshots(client: AsyncClient, db_session: AsyncSession):
    await _seed_snapshot(db_session, "snap-A")
    await _seed_snapshot(db_session, "snap-B")
    r = await client.get("/v1/snapshots", headers=READ_HEADERS)
    assert r.status_code == 200
    titles = {row["title"] for row in r.json()["results"]}
    assert {"snap-A", "snap-B"} <= titles
    assert all("createdDate" in row and "pvCount" in row for row in r.json()["results"])


async def test_list_reports_value_counts(client: AsyncClient, db_session: AsyncSession):
    pv = await _seed_pv(db_session)
    await _seed_snapshot(db_session, "with-value", pv=pv)
    await _seed_snapshot(db_session, "empty")
    r = await client.get("/v1/snapshots", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"]: row["pvCount"] for row in r.json()["results"]} == {"with-value": 1, "empty": 0}


async def test_list_does_not_load_snapshot_values(client: AsyncClient, db_session: AsyncSession, test_engine):
    """Listing must count values in SQL, never fetch the rows — a full machine save holds ~16k."""
    pv = await _seed_pv(db_session)
    await _seed_snapshot(db_session, "with-value", pv=pv)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        r = await client.get("/v1/snapshots", headers=READ_HEADERS)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)

    assert r.status_code == 200, r.text
    value_reads = [s for s in statements if "FROM squirrel.snapshot_value" in s and "count(" not in s]
    assert value_reads == []


async def test_list_filtered_by_title(client: AsyncClient, db_session: AsyncSession):
    await _seed_snapshot(db_session, "alpha")
    await _seed_snapshot(db_session, "beta")
    r = await client.get("/v1/snapshots?title=alph", headers=READ_HEADERS)
    assert {row["title"] for row in r.json()["results"]} == {"alpha"}


async def _seed_days(session: AsyncSession, count: int) -> None:
    """Seed ``count`` snapshots, ``snap-001`` oldest, one day apart from 2026-01-02."""
    for day in range(1, count + 1):
        await _seed_snapshot(
            session, f"snap-{day:03d}", created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
        )


async def _paged(client: AsyncClient, query: str = "", token: str | None = None) -> dict:
    """GET one ``/v1/snapshots`` page, continuing from ``token`` when given."""
    params = f"{query}&continuationToken={token}" if token else query
    r = await client.get(f"/v1/snapshots?{params}", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _titles(page: dict) -> list[str]:
    return [row["title"] for row in page["results"]]


async def test_list_walks_every_snapshot_newest_first(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 7)
    first = await _paged(client, "pageSize=3")
    second = await _paged(client, "pageSize=3", first["continuationToken"])
    third = await _paged(client, "pageSize=3", second["continuationToken"])
    assert _titles(first) == ["snap-007", "snap-006", "snap-005"]
    assert _titles(second) == ["snap-004", "snap-003", "snap-002"]
    assert _titles(third) == ["snap-001"]
    assert third["continuationToken"] is None
    assert first["totalCount"] == 7


async def test_list_exact_multiple_ends_without_an_empty_page(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 4)
    first = await _paged(client, "pageSize=2")
    second = await _paged(client, "pageSize=2", first["continuationToken"])
    assert _titles(second) == ["snap-002", "snap-001"]
    assert second["continuationToken"] is None


async def test_list_default_page_is_100(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 105)
    first = await _paged(client)
    assert len(first["results"]) == 100
    assert _titles(await _paged(client, token=first["continuationToken"]))[0] == "snap-005"


async def test_list_snapshot_created_mid_paging_is_not_repeated(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 6)
    first = await _paged(client, "pageSize=3")
    # Newer than everything: with offset paging this shifts snap-004 onto page two as well.
    await _seed_snapshot(db_session, "new", created_at=datetime(2026, 12, 1, tzinfo=UTC))
    second = await _paged(client, "pageSize=3", first["continuationToken"])
    assert _titles(first) == ["snap-006", "snap-005", "snap-004"]
    assert _titles(second) == ["snap-003", "snap-002", "snap-001"]


async def test_list_snapshot_deleted_mid_paging_skips_nothing(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 6)
    first = await _paged(client, "pageSize=3")
    # Deleting a row already shown: with offset paging snap-003 would slide onto page one, unseen.
    newest = first["results"][0]["id"]
    assert (await client.delete(f"/v1/snapshots/{newest}", headers=WRITE_HEADERS)).status_code == 200
    second = await _paged(client, "pageSize=3", first["continuationToken"])
    assert _titles(second) == ["snap-003", "snap-002", "snap-001"]


async def test_list_cursor_survives_deleting_its_own_row(client: AsyncClient, db_session: AsyncSession):
    await _seed_days(db_session, 4)
    first = await _paged(client, "pageSize=2")
    last_shown = first["results"][-1]["id"]
    assert (await client.delete(f"/v1/snapshots/{last_shown}", headers=WRITE_HEADERS)).status_code == 200
    second = await _paged(client, "pageSize=2", first["continuationToken"])
    assert _titles(second) == ["snap-002", "snap-001"]


async def test_list_pages_are_disjoint_when_created_at_ties(client: AsyncClient, db_session: AsyncSession):
    same_time = datetime(2026, 7, 20, 6, 17, 24, tzinfo=UTC)
    seeded = {await _seed_snapshot(db_session, f"tie-{i}", created_at=same_time) for i in range(7)}
    seen: list[str] = []
    token = None
    for _ in range(3):
        page = await _paged(client, "pageSize=3", token)
        seen.extend(row["id"] for row in page["results"])
        token = page["continuationToken"]
    assert token is None
    assert len(seen) == 7
    assert set(seen) == seeded


async def test_list_paging_composes_with_filters(client: AsyncClient, db_session: AsyncSession):
    for i in range(4):
        await _seed_snapshot(db_session, f"keep-{i}", created_at=datetime(2026, 2, 1 + i, tzinfo=UTC))
        await _seed_snapshot(db_session, f"drop-{i}", created_at=datetime(2026, 3, 1 + i, tzinfo=UTC))
    query = "title=keep&end=2026-02-03T12:00:00Z&pageSize=2"
    first = await _paged(client, query)
    second = await _paged(client, query, first["continuationToken"])
    assert _titles(first) == ["keep-2", "keep-1"]
    assert _titles(second) == ["keep-0"]
    assert first["totalCount"] == 3


async def test_list_empty_result(client: AsyncClient):
    page = await _paged(client)
    assert page == {"results": [], "continuationToken": None, "totalCount": 0}


async def test_list_rejects_malformed_token(client: AsyncClient):
    for token in ("not-a-token", "bm9waXBl", "MjAyNi0wMS0wMVQwMDowMDowMHxub3QtYS11dWlk", "%%%"):
        r = await client.get(f"/v1/snapshots?continuationToken={token}", headers=READ_HEADERS)
        assert r.status_code == 400, token


async def test_list_rejects_out_of_range_page_size(client: AsyncClient):
    for query in ("pageSize=0", "pageSize=1001"):
        r = await client.get(f"/v1/snapshots?{query}", headers=READ_HEADERS)
        assert r.status_code == 422, query


async def _seed_early_and_late(session: AsyncSession) -> None:
    """Seed one snapshot created in January and one created in June of 2026."""
    await _seed_snapshot(session, "early", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await _seed_snapshot(session, "late", created_at=datetime(2026, 6, 1, tzinfo=UTC))


async def test_list_filtered_by_start(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?start=2026-03-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()["results"]} == {"late"}


async def test_list_filtered_by_end(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?end=2026-03-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()["results"]} == {"early"}


async def test_list_filtered_by_start_and_end(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?start=2026-05-01T00:00:00Z&end=2026-07-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()["results"]} == {"late"}

    r = await client.get("/v1/snapshots?start=2026-07-01T00:00:00Z&end=2026-08-01T00:00:00Z", headers=READ_HEADERS)
    assert r.json()["results"] == []


async def test_list_datetime_filter_accepts_naive_and_offset_forms(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    naive = await client.get("/v1/snapshots?start=2026-03-01T00:00:00", headers=READ_HEADERS)
    offset = await client.get("/v1/snapshots?start=2026-02-28T19:00:00-05:00", headers=READ_HEADERS)
    assert naive.status_code == 200, naive.text
    assert offset.status_code == 200, offset.text
    assert {row["title"] for row in naive.json()["results"]} == {"late"}
    assert {row["title"] for row in offset.json()["results"]} == {"late"}


async def test_list_rejects_malformed_datetime(client: AsyncClient):
    assert (await client.get("/v1/snapshots?start=not-a-date", headers=READ_HEADERS)).status_code == 422
    assert (await client.get("/v1/snapshots?end=not-a-date", headers=READ_HEADERS)).status_code == 422


async def test_list_rejects_date_only_bounds(client: AsyncClient):
    for query in ("start=2026-01-01", "end=2026-01-31"):
        r = await client.get(f"/v1/snapshots?{query}", headers=READ_HEADERS)
        assert r.status_code == 422, r.text
        assert "not a bare date" in r.text


async def test_list_rejects_start_after_end(client: AsyncClient):
    r = await client.get("/v1/snapshots?start=2026-07-01T00:00:00Z&end=2026-05-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "bad_request"


async def test_list_rejects_bounds_that_overflow_on_utc_conversion(client: AsyncClient):
    for query in ("start=9999-12-31T23:00:00-05:00", "end=0001-01-01T00:00:00%2B05:00"):
        r = await client.get(f"/v1/snapshots?{query}", headers=READ_HEADERS)
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "bad_request"


async def test_get_snapshot_with_values(client: AsyncClient, db_session: AsyncSession):
    pv = await _seed_pv(db_session)
    sid = await _seed_snapshot(db_session, "with-values", pv=pv, value=7.0)
    r = await client.get(f"/v1/snapshots/{sid}", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"pvValues", "pvCount"} <= body.keys()  # camelCase
    assert body["pvCount"] == 1
    assert body["pvValues"][0]["setpointValue"]["value"] == 7.0


async def test_get_snapshot_bad_and_missing_uuid(client: AsyncClient):
    assert (await client.get("/v1/snapshots/not-a-uuid", headers=READ_HEADERS)).status_code == 404
    r = await client.get("/v1/snapshots/00000000-0000-0000-0000-000000000000", headers=READ_HEADERS)
    assert r.status_code == 404 and r.json()["code"] == "snapshot_not_found"


async def test_update_metadata(client: AsyncClient, db_session: AsyncSession):
    sid = await _seed_snapshot(db_session, "orig")
    r = await client.put(f"/v1/snapshots/{sid}", headers=WRITE_HEADERS, json={"title": "new", "description": "d"})
    assert r.status_code == 200
    assert r.json()["title"] == "new" and r.json()["description"] == "d"


async def test_compare_self_has_no_differences(client: AsyncClient, db_session: AsyncSession):
    pv = await _seed_pv(db_session)
    sid = await _seed_snapshot(db_session, "self", pv=pv)
    r = await client.get(f"/v1/snapshots/{sid}/compare/{sid}", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"matchCount", "differenceCount", "differences"} <= body.keys()  # camelCase
    assert body["differenceCount"] == 0


async def test_compare_missing_is_404(client: AsyncClient, db_session: AsyncSession):
    sid = await _seed_snapshot(db_session, "only")
    r = await client.get(f"/v1/snapshots/{sid}/compare/00000000-0000-0000-0000-000000000000", headers=READ_HEADERS)
    assert r.status_code == 404


async def test_delete_snapshot(client: AsyncClient, db_session: AsyncSession):
    sid = await _seed_snapshot(db_session, "doomed")
    assert (await client.delete(f"/v1/snapshots/{sid}", headers=WRITE_HEADERS)).status_code == 200
    assert (await client.get(f"/v1/snapshots/{sid}", headers=READ_HEADERS)).status_code == 404


# --- enqueue wiring (stubbed) ----------------------------------------------- #


async def test_create_enqueues_job(client: AsyncClient, enqueued: list):
    r = await client.post("/v1/snapshots", headers=WRITE_HEADERS, json={"title": "T", "description": "D"})
    assert r.status_code == 200
    assert r.json() == {"jobId": "test-job-id", "statusUrl": "/jobs/v1/test-job-id"}
    assert enqueued[0][0] == "create_snapshot_task"
    assert enqueued[0][2] == {"title": "T", "description": "D"}


async def test_restore_existing_enqueues_with_pv_ids(client: AsyncClient, db_session: AsyncSession, enqueued: list):
    sid = await _seed_snapshot(db_session, "restore-me")
    r = await client.post(f"/v1/snapshots/{sid}/restore", headers=WRITE_HEADERS, json={"pvIds": ["p1", "p2"]})
    assert r.status_code == 200 and r.json()["jobId"] == "test-job-id"
    assert enqueued[-1][0] == "restore_snapshot_task"
    assert enqueued[-1][2] == {"snapshot_id": sid, "pv_ids": ["p1", "p2"]}


async def test_restore_missing_is_404_and_does_not_enqueue(client: AsyncClient, enqueued: list):
    r = await client.post("/v1/snapshots/00000000-0000-0000-0000-000000000000/restore", headers=WRITE_HEADERS, json={})
    assert r.status_code == 404
    assert enqueued == []


async def test_auth_gating(client: AsyncClient):
    assert (await client.get("/v1/snapshots")).status_code in (401, 403)
    assert (await client.post("/v1/snapshots", headers=READ_HEADERS, json={"title": "x"})).status_code == 403
