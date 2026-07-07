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


async def _seed_snapshot(session: AsyncSession, title: str, *, pv: Pv | None = None, value: float = 42.0) -> str:
    snap = Snapshot(title=title, description="seeded")
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
    await session.commit()  # request sessions are separate — committed rows must be visible
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
