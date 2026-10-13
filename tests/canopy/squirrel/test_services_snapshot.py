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
from canopy.squirrel.schemas.snapshot import NewSnapshotDTO, RestoreRequestDTO
from canopy.squirrel.adapters.epics_io import EpicsValue
from canopy.squirrel.restore_hooks.base import RestoreHook
from canopy.squirrel.restore_hooks.magnet_trim import MagnetTrimHook
from canopy.squirrel.services.snapshot_service import SnapshotService
from canopy.squirrel.restore_hooks.write_to_con import WriteToConHook


class _NoHook(RestoreHook):
    """Explicit no-op hook so tests don't fall through to the env-configured singletons."""

    def derive_extra_writes(self, address: str, value: Any) -> list[tuple[str, Any]]:
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
        if cb and values:
            await cb(len(values), len(values), f"Wrote {len(values)}/{len(values)} PVs...")
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
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])

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
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])

    summary = await svc.create_snapshot(NewSnapshotDTO(title="snap"))
    full = await svc.get_by_id(summary.id)
    by_name = {v.pv_name: v for v in full.pv_values}
    assert by_name["OK:SP"].setpoint_value.value == 1.0
    assert by_name["DEAD:SP"].setpoint_value is None  # disconnected read stored no blob


async def test_paginated_get_does_not_delete_values(db_session: AsyncSession):
    """Regression: a paginated read must never orphan-delete the off-page values.

    ``Snapshot.values`` is ``cascade="all, delete-orphan"``; assigning a partial page to
    that mapped collection (the old ``get_with_values``) would delete the rest on flush.
    """
    for i in range(3):
        await _seed_pv(db_session, f"P{i}:SP")
    io = FakeEpicsIO({f"P{i}:SP": _ev(float(i)) for i in range(3)})
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="snap"))
    assert summary.pv_count == 3

    page = await svc.get_by_id(summary.id, limit=1)
    assert page is not None and len(page.pv_values) == 1  # only one value on the page
    assert page.pv_count == 3  # but the total is still reported correctly

    await db_session.flush()  # would flush any pending orphan deletes
    assert await svc.value_repo.count_by_snapshot(summary.id) == 3  # all values still present


async def test_restore_writes_setpoints_only(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP", "A:RB")
    io = FakeEpicsIO({"A:SP": _ev(5.0), "A:RB": _ev(4.9)})
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert result.total_pvs == 1 and result.success_count == 1 and result.failure_count == 0
    assert io.writes == {"A:SP": 5.0}  # readback never written


async def test_restore_magnet_trims_after_primary(db_session: AsyncSession):
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    svc = SnapshotService(db_session, io, restore_hooks=[MagnetTrimHook()])
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
    svc = SnapshotService(db_session, io, restore_hooks=[MagnetTrimHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert "QUAD:LI21:201:CTRL" not in io.writes  # no trim when the setpoint write failed
    assert result.failure_count == 1


async def test_restore_reports_write_failure(db_session: AsyncSession):
    pv = await _seed_pv(db_session, "A:SP")
    io = FakeEpicsIO({"A:SP": _ev(5.0)})
    io.fail_writes.add("A:SP")
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    result = await svc.restore_snapshot(summary.id)
    assert result.failure_count == 1
    assert result.failures[0]["pvName"] == "A:SP"
    assert result.failures[0]["pvId"] == pv.id


async def test_restore_runs_every_hook_in_order(db_session: AsyncSession):
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    svc = SnapshotService(db_session, io, restore_hooks=[WriteToConHook(), MagnetTrimHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert list(io.writes.items()) == [
        ("QUAD:LI21:201:BDES", 3.0),
        ("QUAD:LI21:201:BCON", 3.0),
        ("QUAD:LI21:201:CTRL", "TRIM"),
    ]
    assert result.success_count == 1 and result.failure_count == 0


async def test_restore_attributes_hook_write_failure_to_the_hook(db_session: AsyncSession):
    """A hook's failed write fails its primary PV and names that hook as the phase."""
    pv = await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    io.fail_writes.add("QUAD:LI21:201:BCON")
    svc = SnapshotService(db_session, io, restore_hooks=[MagnetTrimHook(), WriteToConHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert io.writes["QUAD:LI21:201:CTRL"] == "TRIM"
    assert result.success_count == 0 and result.failure_count == 1
    failure = result.failures[0]
    assert failure["pvId"] == pv.id
    assert failure["pvName"] == "QUAD:LI21:201:BDES"
    assert failure["phase"] == "WriteToConHook"
    assert "QUAD:LI21:201:BCON" in failure["error"]


async def test_restore_skips_later_hooks_after_an_earlier_hook_fails(db_session: AsyncSession):
    """A failed BCON write leaves the magnet untrimmed."""
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0)})
    io.fail_writes.add("QUAD:LI21:201:BCON")
    svc = SnapshotService(db_session, io, restore_hooks=[WriteToConHook(), MagnetTrimHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert io.writes == {"QUAD:LI21:201:BDES": 3.0}
    assert result.failure_count == 1
    assert result.failures[0]["phase"] == "WriteToConHook"
    assert "QUAD:LI21:201:BCON" in result.failures[0]["error"]


async def test_restore_attributes_primary_failure_to_setpoints(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP")
    io = FakeEpicsIO({"A:SP": _ev(5.0)})
    io.fail_writes.add("A:SP")
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    result = await svc.restore_snapshot(summary.id)
    assert result.failures[0]["phase"] == "Setpoints"


async def test_restore_derivation_error_skips_the_pv_and_blames_setpoints(db_session: AsyncSession):
    """A PV a hook cannot derive from is never written, so nothing is left half-restored."""
    await _seed_pv(db_session, "QUAD:LI21:201:RBV")
    io = FakeEpicsIO({"QUAD:LI21:201:RBV": _ev(3.0)})
    svc = SnapshotService(db_session, io, restore_hooks=[MagnetTrimHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id)
    assert io.writes == {}
    assert result.failure_count == 1
    assert result.failures[0]["phase"] == "Setpoints"
    assert "not a BDES setpoint" in result.failures[0]["error"]


async def test_restore_progress_reaches_the_total(db_session: AsyncSession):
    """Progress totals 2 setpoints + 2 BCON writes + 2 trims.

    Extras skipped after a failed setpoint (BEND) or a failed earlier hook (QUAD's
    trim, after its BCON write fails) count as done.
    """
    await _seed_pv(db_session, "QUAD:LI21:201:BDES")
    await _seed_pv(db_session, "BEND:DMP1:400:BDES")
    io = FakeEpicsIO({"QUAD:LI21:201:BDES": _ev(3.0), "BEND:DMP1:400:BDES": _ev(4.0)})
    io.fail_writes.update({"BEND:DMP1:400:BDES", "QUAD:LI21:201:BCON"})
    svc = SnapshotService(db_session, io, restore_hooks=[WriteToConHook(), MagnetTrimHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="m"))

    reported: list[tuple[int, int]] = []

    async def on_progress(current: int, total: int, message: str) -> None:
        reported.append((current, total))

    await svc.restore_snapshot(summary.id, progress_callback=on_progress)

    totals = {total for _current, total in reported}
    assert totals == {6}
    currents = [current for current, _total in reported]
    assert currents == sorted(currents)
    assert currents[0] == 0
    assert reported[-1] == (6, 6)


async def test_restore_subset_by_pv_ids(db_session: AsyncSession):
    pv_a = await _seed_pv(db_session, "A:SP")
    await _seed_pv(db_session, "B:SP")
    io = FakeEpicsIO({"A:SP": _ev(1.0), "B:SP": _ev(2.0)})
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])
    summary = await svc.create_snapshot(NewSnapshotDTO(title="s"))

    io.writes.clear()
    result = await svc.restore_snapshot(summary.id, RestoreRequestDTO(pv_ids=[pv_a.id]))
    assert result.total_pvs == 1
    assert io.writes == {"A:SP": 1.0}  # only the requested PV


async def test_compare_detects_value_difference(db_session: AsyncSession):
    await _seed_pv(db_session, "A:SP", abs_tol=0.0, rel_tol=0.0)
    svc1 = SnapshotService(db_session, FakeEpicsIO({"A:SP": _ev(1.0)}), restore_hooks=[_NoHook()])
    s1 = await svc1.create_snapshot(NewSnapshotDTO(title="s1"))
    svc2 = SnapshotService(db_session, FakeEpicsIO({"A:SP": _ev(2.0)}), restore_hooks=[_NoHook()])
    s2 = await svc2.create_snapshot(NewSnapshotDTO(title="s2"))

    cmp = await svc1.compare_snapshots(s1.id, s2.id)
    assert cmp.difference_count == 1 and cmp.match_count == 0

    cmp_same = await svc1.compare_snapshots(s1.id, s1.id)
    assert cmp_same.difference_count == 0  # a snapshot always matches itself


async def test_create_and_restore_roundtrip_waveform_value(db_session: AsyncSession):
    await _seed_pv(db_session, "WF:SP")
    io = FakeEpicsIO({"WF:SP": _ev([1.0, 2.0, 3.0])})
    svc = SnapshotService(db_session, io, restore_hooks=[_NoHook()])

    summary = await svc.create_snapshot(NewSnapshotDTO(title="wf"))
    full = await svc.get_by_id(summary.id)
    by_name = {v.pv_name: v for v in full.pv_values}
    assert by_name["WF:SP"].setpoint_value.value == [1.0, 2.0, 3.0]  # list round-trips through JSONB

    io.writes.clear()
    await svc.restore_snapshot(summary.id)
    assert io.writes == {"WF:SP": [1.0, 2.0, 3.0]}  # written back unchanged


async def test_compare_waveforms_within_tolerance_match(db_session: AsyncSession):
    await _seed_pv(db_session, "WF:SP", abs_tol=0.1, rel_tol=0.0)
    svc1 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.0, 2.0, 3.0])}), restore_hooks=[_NoHook()])
    s1 = await svc1.create_snapshot(NewSnapshotDTO(title="s1"))
    svc2 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.05, 2.0, 3.0])}), restore_hooks=[_NoHook()])
    s2 = await svc2.create_snapshot(NewSnapshotDTO(title="s2"))

    cmp = await svc1.compare_snapshots(s1.id, s2.id)
    assert cmp.difference_count == 0 and cmp.match_count == 1  # within abs_tolerance element-wise


async def test_compare_waveforms_outside_tolerance_on_one_element_differs(db_session: AsyncSession):
    await _seed_pv(db_session, "WF:SP", abs_tol=0.1, rel_tol=0.0)
    svc1 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.0, 2.0, 3.0])}), restore_hooks=[_NoHook()])
    s1 = await svc1.create_snapshot(NewSnapshotDTO(title="s1"))
    svc2 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.0, 2.5, 3.0])}), restore_hooks=[_NoHook()])
    s2 = await svc2.create_snapshot(NewSnapshotDTO(title="s2"))

    cmp = await svc1.compare_snapshots(s1.id, s2.id)
    assert cmp.difference_count == 1 and cmp.match_count == 0  # one element outside tolerance is enough


async def test_compare_waveforms_of_different_length_differs(db_session: AsyncSession):
    await _seed_pv(db_session, "WF:SP", abs_tol=0.1, rel_tol=0.0)
    svc1 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.0, 2.0, 3.0])}), restore_hooks=[_NoHook()])
    s1 = await svc1.create_snapshot(NewSnapshotDTO(title="s1"))
    svc2 = SnapshotService(db_session, FakeEpicsIO({"WF:SP": _ev([1.0, 2.0])}), restore_hooks=[_NoHook()])
    s2 = await svc2.create_snapshot(NewSnapshotDTO(title="s2"))

    cmp = await svc1.compare_snapshots(s1.id, s2.id)
    assert cmp.difference_count == 1  # length mismatch reports as a difference, not an error


def test_values_within_tolerance_both_none_matches():
    assert SnapshotService._values_within_tolerance(None, None, 0.0, 0.0) is True


def test_values_within_tolerance_one_none_differs():
    assert SnapshotService._values_within_tolerance(1.0, None, 0.0, 0.0) is False
    assert SnapshotService._values_within_tolerance(None, 1.0, 0.0, 0.0) is False


def test_values_within_tolerance_list_vs_scalar_differs():
    assert SnapshotService._values_within_tolerance([1.0, 2.0], 1.0, 0.0, 0.0) is False
    assert SnapshotService._values_within_tolerance(1.0, [1.0, 2.0], 0.0, 0.0) is False
