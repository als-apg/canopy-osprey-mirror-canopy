"""Typed `Annotated[..., Depends(...)]` aliases for Raccoon's API layer.

Routes pull these in by alias rather than re-spelling `Depends()`.
"""

from typing import Annotated

from fastapi import Depends

from canopy.raccoon import gateway
from canopy.raccoon.services.directory_service import DirectoryService


def get_directory_service() -> DirectoryService:
    return gateway.get_service()


DirectoryServiceDep = Annotated[DirectoryService, Depends(get_directory_service)]
