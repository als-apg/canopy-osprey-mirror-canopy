"""Typed ``Annotated[..., Depends(...)]`` alias for the facility routes."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import get_session
from canopy.core.facilities.service import FacilityService

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_facility_service(session: SessionDep) -> FacilityService:
    return FacilityService(session)


FacilityServiceDep = Annotated[FacilityService, Depends(get_facility_service)]
