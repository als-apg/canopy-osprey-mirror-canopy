"""API tests for the snapshot endpoints.

Create/restore enqueue Arq jobs (``jobs.gateway.enqueue`` is stubbed by the ``client``
fixture, which records calls in ``enqueued``). Read/compare/delete run inline, so those
snapshots are seeded directly through the shared test session.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
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
    titles = {row["title"] for row in r.json()}
    assert {"snap-A", "snap-B"} <= titles
    assert all("createdDate" in row and "pvCount" in row for row in r.json())


async def test_list_filtered_by_title(client: AsyncClient, db_session: AsyncSession):
    await _seed_snapshot(db_session, "alpha")
    await _seed_snapshot(db_session, "beta")
    r = await client.get("/v1/snapshots?title=alph", headers=READ_HEADERS)
    assert {row["title"] for row in r.json()} == {"alpha"}


async def _seed_early_and_late(session: AsyncSession) -> None:
    """Seed one snapshot created in January and one created in June of 2026."""
    await _seed_snapshot(session, "early", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await _seed_snapshot(session, "late", created_at=datetime(2026, 6, 1, tzinfo=UTC))


async def test_list_filtered_by_start(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?start=2026-03-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()} == {"late"}


async def test_list_filtered_by_end(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?end=2026-03-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()} == {"early"}


async def test_list_filtered_by_start_and_end(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    r = await client.get("/v1/snapshots?start=2026-05-01T00:00:00Z&end=2026-07-01T00:00:00Z", headers=READ_HEADERS)
    assert r.status_code == 200, r.text
    assert {row["title"] for row in r.json()} == {"late"}

    r = await client.get("/v1/snapshots?start=2026-07-01T00:00:00Z&end=2026-08-01T00:00:00Z", headers=READ_HEADERS)
    assert r.json() == []


async def test_list_datetime_filter_accepts_naive_and_offset_forms(client: AsyncClient, db_session: AsyncSession):
    await _seed_early_and_late(db_session)
    naive = await client.get("/v1/snapshots?start=2026-03-01T00:00:00", headers=READ_HEADERS)
    offset = await client.get("/v1/snapshots?start=2026-02-28T19:00:00-05:00", headers=READ_HEADERS)
    assert naive.status_code == 200, naive.text
    assert offset.status_code == 200, offset.text
    assert {row["title"] for row in naive.json()} == {"late"}
    assert {row["title"] for row in offset.json()} == {"late"}


async def test_list_rejects_malformed_datetime(client: AsyncClient):
    assert (await client.get("/v1/snapshots?start=not-a-date", headers=READ_HEADERS)).status_code == 422
    assert (await client.get("/v1/snapshots?end=not-a-date", headers=READ_HEADERS)).status_code == 422


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
