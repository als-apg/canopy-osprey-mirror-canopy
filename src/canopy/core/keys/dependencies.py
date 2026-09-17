"""Typed ``Annotated[..., Depends(...)]`` alias for the API key management routes."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import get_session
from canopy.core.keys.service import ApiKeyService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_api_key_service(session: SessionDep) -> ApiKeyService:
    return ApiKeyService(session)


ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
