"""Typed ``Annotated[..., Depends(...)]`` aliases for Squirrel's API layer.

Each service is built per request from the shared ``canopy.core.db.SessionDep``.
``SnapshotService`` is constructed without an ``EpicsIO``: the read endpoints never touch
EPICS (snapshot create/restore run in the worker, which injects the connector).
"""

from typing import Annotated

from fastapi import Depends

from canopy.core.db import SessionDep
from canopy.squirrel.services.pv_service import PvService
from canopy.squirrel.services.tag_service import TagService
from canopy.squirrel.services.snapshot_service import SnapshotService


def get_snapshot_service(session: SessionDep) -> SnapshotService:
    return SnapshotService(session)


def get_pv_service(session: SessionDep) -> PvService:
    return PvService(session)


def get_tag_service(session: SessionDep) -> TagService:
    return TagService(session)


SnapshotServiceDep = Annotated[SnapshotService, Depends(get_snapshot_service)]
PvServiceDep = Annotated[PvService, Depends(get_pv_service)]
TagServiceDep = Annotated[TagService, Depends(get_tag_service)]
