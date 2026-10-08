"""Public surface of the Squirrel module for in-process callers.

Other Canopy modules import only from this file; the rest of ``canopy.squirrel.*``
is private implementation. Unlike fox/converter — whose gateways hand back a
service *singleton* bound at startup — Squirrel's services are request/session
scoped (there is nothing to bind), so each call here opens a short-lived read
session via ``session_scope`` and builds the service from it.

Today nothing imports Squirrel across a module boundary: the frontend talks to it
over HTTP at ``/squirrel/v1``. This file defines the intended public read surface
now so the first in-process consumer (and the boundary linters on the roadmap) has
one stable entry point instead of reaching into the internals.
"""

from __future__ import annotations

from canopy.core.db import session_scope
from canopy.squirrel.schemas.pv import PVElementDTO
from canopy.squirrel.schemas.snapshot import SnapshotDTO
from canopy.squirrel.services.pv_service import PvService
from canopy.squirrel.services.snapshot_service import SnapshotService


async def get_snapshot(snapshot_id: str, *, limit: int | None = None, offset: int = 0) -> SnapshotDTO | None:
    """Return a stored snapshot with its values (optionally paginated), or ``None`` if absent.

    Read-only: the snapshot is loaded and mapped to a DTO inside a short-lived session
    that is disposed before returning.
    """
    async with session_scope() as session:
        return await SnapshotService(session).get_by_id(snapshot_id, limit=limit, offset=offset)


async def get_pv(pv_id: str) -> PVElementDTO | None:
    """Return a registered PV by id, or ``None`` if no such PV. Read-only."""
    async with session_scope() as session:
        return await PvService(session).get_by_id(pv_id)
