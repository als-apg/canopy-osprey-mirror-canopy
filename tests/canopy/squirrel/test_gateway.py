"""Tests for the squirrel module's public gateway surface.

Each gateway call opens its own ``session_scope``; here we patch that to hand back the
seeded test session so the read runs against the test DB. This is also the only place
``PvService.get_by_id`` is exercised — it exists solely as ``gateway.get_pv``'s impl.
"""

import contextlib

from sqlalchemy.ext.asyncio import AsyncSession

from canopy.squirrel import gateway
from canopy.squirrel.data.models import Pv, Snapshot

# A well-formed but absent UUID: the read must return None, not raise.
_MISSING_ID = "00000000-0000-0000-0000-000000000000"


def _patch_scope(monkeypatch, session: AsyncSession) -> None:
    @contextlib.asynccontextmanager
    async def _scope():
        yield session

    # Patch the name bound in the gateway module (it did ``from ... import session_scope``).
    monkeypatch.setattr(gateway, "session_scope", _scope)


async def test_gateway_get_pv(db_session: AsyncSession, monkeypatch):
    pv = Pv(setpoint_address="G:SP", readback_address="G:RB", device="DEV", abs_tolerance=0.0, rel_tolerance=0.0)
    db_session.add(pv)
    await db_session.flush()
    _patch_scope(monkeypatch, db_session)

    got = await gateway.get_pv(pv.id)
    assert got is not None
    assert str(got.id) == str(pv.id)
    assert got.setpoint_address == "G:SP"

    assert await gateway.get_pv(_MISSING_ID) is None


async def test_gateway_get_snapshot(db_session: AsyncSession, monkeypatch):
    snap = Snapshot(title="G-snap", description=None, created_by=None)
    db_session.add(snap)
    await db_session.flush()
    _patch_scope(monkeypatch, db_session)

    got = await gateway.get_snapshot(snap.id)
    assert got is not None
    assert str(got.id) == str(snap.id)
    assert got.pv_count == 0
    assert got.pv_values == []

    assert await gateway.get_snapshot(_MISSING_ID) is None
