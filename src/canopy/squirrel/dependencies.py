"""Typed ``Annotated[..., Depends(...)]`` aliases for Squirrel's API layer.

Each service is built per request from a ``get_session`` dependency (which commits on
success, rolls back on error). It is function-scoped so the commit lands before the
response is sent.
``SnapshotService`` is constructed without an ``EpicsIO``: the read endpoints never touch
EPICS (snapshot create/restore run in the worker, which injects the connector).
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import get_session
from canopy.squirrel.services.pv_service import PvService
from canopy.squirrel.services.tag_service import TagService
from canopy.squirrel.services.snapshot_service import SnapshotService

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_snapshot_service(session: SessionDep) -> SnapshotService:
    return SnapshotService(session)


def get_pv_service(session: SessionDep) -> PvService:
    return PvService(session)


def get_tag_service(session: SessionDep) -> TagService:
    return TagService(session)


SnapshotServiceDep = Annotated[SnapshotService, Depends(get_snapshot_service)]
PvServiceDep = Annotated[PvService, Depends(get_pv_service)]
TagServiceDep = Annotated[TagService, Depends(get_tag_service)]
