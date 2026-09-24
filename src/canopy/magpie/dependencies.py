"""Typed ``Annotated[..., Depends(...)]`` aliases for Magpie's API layer.

The service is built per request from a ``get_session`` dependency (which commits on
success, rolls back on error). It is function-scoped so the commit lands before the
response is sent.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import get_session
from canopy.magpie.services.config_service import ConfigService

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_config_service(session: SessionDep) -> ConfigService:
    return ConfigService(session)


ConfigServiceDep = Annotated[ConfigService, Depends(get_config_service)]
