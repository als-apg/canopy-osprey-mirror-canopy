"""Integration tests for SnapshotService against the real test DB + a fake EpicsIO.

These exercise the worker-side domain logic — snapshot create (the COPY bulk-insert path),
restore invariants (setpoints-only, magnet-trim-after-primary, failure attribution, subset),
and compare — without IOCs. The service and seed data share one ``db_session`` transaction,
so reads after writes are visible without committing; ``drop_all`` isolates across tests.
"""

from typing import Any
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from canopy.squirrel.data.models import Pv
from canopy.squirrel.adapters.epics_io import EpicsValue
from canopy.squirrel.restore_hooks.base import RestoreHook
from canopy.squirrel.schemas.snapshot import NewSnapshotDTO, RestoreRequestDTO
from canopy.squirrel.restore_hooks.magnet_trim import MagnetTrimHook
from canopy.squirrel.services.snapshot_service import SnapshotService


class _NoHook(RestoreHook):
    """Explicit no-op hook so tests don't fall through to the env-configured singleton."""

    def derive_extra_writes(self, address: str) -> list[tuple[str, Any]]:
        return []


class FakeEpicsIO:
    """In-memory stand-in for the EpicsIO adapter (records writes, serves canned reads)."""

    def __init__(self, reads: dict[str, EpicsValue] | None = None):
        self.reads = reads or {}
        self.writes: dict[str, Any] = {}
        self.fail_writes: set[str] = set()

    async def get_many(self, addrs: list[str]) -> dict[str, EpicsValue]:
        return {a: self.reads.get(a, EpicsValue(value=None, connected=False, error="no value")) for a in addrs}

    async def get_many_with_progress(self, addrs, cb=None) -> dict[str, EpicsValue]:
        return await self.get_many(addrs)

    async def put_many(self, values: dict[str, Any]) -> dict[str, tuple[bool, str | None]]:
        return await self.put_many_with_progress(values, None)

    async def put_many_with_progress(self, values, cb=None) -> dict[str, tuple[bool, str | None]]:
        out: dict[str, tuple[bool, str | None]] = {}
        for addr, val in values.items():
            if addr in self.fail_writes:
                out[addr] = (False, "write failed")
            else:
                self.writes[addr] = val
                out[addr] = (True, None)
        return out


def _ev(value: Any, severity: int = 0) -> EpicsValue:
    return EpicsValue(value=value, severity=severity, timestamp=datetime.now(UTC), connected=True)


async def _seed_pv(session: AsyncSession, setpoint, readback=None, *, abs_tol=0.0, rel_tol=0.0) -> Pv:
    pv = Pv(
        setpoint_address=setpoint,
        readback_address=readback,
        device="DEV",
        abs_tolerance=abs_tol,
        rel_tolerance=rel_tol,
    )
    session.add(pv)
    await session.flush()
    return pv


async def test_create_snapshot_persists_values_via_copy(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP", "A:RB")
    await _seed_pv(db_session, "B:SP")
    io = FakeEpicsIO({"A:SP": _ev(1.0), "A:RB": _ev(0.9), "B:SP": _ev(2.0)})
    svc = SnapshotService(db_session, io, restore_hook=_NoHook())

    summary = await svc.create_snapshot(NewSnapshotDTO(title="snap"))
    assert summary.pv_count == 2

    full = await svc.get_by_id(summary.id)
    assert full is not None and full.pv_count == 2
    by_name = {v.pv_name: v for v in full.pv_values}
    assert by_name["A:SP"].setpoint_value.value == 1.0  # COPY'd JSONB read back
    assert by_name["A:SP"].readback_value.value == 0.9
    assert by_name["B:SP"].setpoint_value.value == 2.0


async def test_create_skips_disconnected_reads(db_session: AsyncSession):
    await _seed_pv(db_session, "OK:SP")
    await _seed_pv(db_session, "DEAD:SP")
    io = FakeEpicsIO({"OK:SP": _ev(1.0)})  # DEAD:SP -> default disconnected EpicsValue
    svc = SnapshotService(db_session, io, restore_hook=_NoHook())

    summary = await svc.create_snapshot(NewSnapshotDTO(title="snap"))
    full = await svc.get_by_id(summary.id)
    by_name = {v.pv_name: v for v in full.pv_values}
    assert by_name["OK:SP"].setpoint_value.value == 1.0
    assert by_name["DEAD:SP"].setpoint_value is None  # disconnected read stored no blob


async def test_restore_writes_setpoints_only(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP", "A:RB")
    io = FakeEpicsIO({"A:SP": _ev(5.0), "A:RB": _ev(4.9)})
    svc = SnapshotService(db_session, io, restore_hook=_NoHook())
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert result.total_pvs == 1 and result.success_count == 1 and result.failure_count == 0
    assert io.writes == {"A:SP": 5.0}  # readback never written


async def test_restore_magnet_trims_after_primary(db_session: AsyncSession):
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    svc = SnapshotService(db_session, io, restore_hook=MagnetTrimHook())
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert io.writes["QUAD:LI21:201:BDES"] == 3.0
    assert io.writes["QUAD:LI21:201:CTRL"] == "TRIM"  # trim derived + written after the BDES setpoint
    assert result.success_count == 1


async def test_restore_does_not_trim_when_primary_fails(db_session: AsyncSession):
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    io.fail_writes.add("QUAD:LI21:201:BDES")
    svc = SnapshotService(db_session, io, restore_hook=MagnetTrimHook())
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert "QUAD:LI21:201:CTRL" not in io.writes  # no trim when the setpoint write failed
    assert result.failure_count == 1


async def test_restore_reports_write_failure(db_session: AsyncSession):
    pv = await _seed_pv(db_session, "A:SP")
    io = FakeEpicsIO({"A:SP": _ev(5.0)})
    io.fail_writes.add("A:SP")
    svc = SnapshotService(db_session, io, restore_hook=_NoHook())
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    result = await svc.restore_snapshot(summary.id)
    assert result.failure_count == 1
    assert result.failures[0]["pvName"] == "A:SP"
    assert result.failures[0]["pvId"] == pv.id


async def test_restore_subset_by_pv_ids(db_session: AsyncSession):
    pv_a = await _seed_pv(db_session, "A:SP")
    await _seed_pv(db_session, "B:SP")
    io = FakeEpicsIO({"A:SP": _ev(1.0), "B:SP": _ev(2.0)})
    svc = SnapshotService(db_session, io, restore_hook=_NoHook())
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id, RestoreRequestDTO(pv_ids=[pv_a.id]))
    assert result.total_pvs == 1
    assert io.writes == {"A:SP": 1.0}  # only the requested PV


async def test_compare_detects_value_difference(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP", abs_tol=0.0, rel_tol=0.0)
    svc1 = SnapshotService(db_session, FakeEpicsIO({"A:SP": _ev(1.0)}), restore_hook=_NoHook())
    s1 = await svc1.create_snapshot(NewSnapshotDTO(title="s1"))
    svc2 = SnapshotService(db_session, FakeEpicsIO({"A:SP": _ev(2.0)}), restore_hook=_NoHook())
    s2 = await svc2.create_snapshot(NewSnapshotDTO(title="s2"))

    cmp = await svc1.compare_snapshots(s1.id, s2.id)
    assert cmp.difference_count == 1 and cmp.match_count == 0

    cmp_same = await svc1.compare_snapshots(s1.id, s1.id)
    assert cmp_same.difference_count == 0  # a snapshot always matches itself
