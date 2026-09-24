"""Typed ``Annotated[..., Depends(...)]`` aliases for Magpie's API layer.

The service is built per request from the shared ``canopy.core.db.SessionDep``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.core.db import SessionDep
from canopy.magpie.services.config_service import ConfigService


def get_config_service(session: SessionDep) -> ConfigService:
    return ConfigService(session)


ConfigServiceDep = Annotated[ConfigService, Depends(get_config_service)]
