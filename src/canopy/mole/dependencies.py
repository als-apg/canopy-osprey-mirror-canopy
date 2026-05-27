"""Typed `Annotated[..., Depends(...)]` aliases for Mole's API layer.

Routes pull these in by alias rather than re-spelling `Depends()`.
"""

from typing import Annotated

from fastapi import Depends

from canopy.mole import gateway
from canopy.mole.services.archiver_service import ArchiverService


def get_archiver_service() -> ArchiverService:
    return gateway.get_service()


ArchiverServiceDep = Annotated[ArchiverService, Depends(get_archiver_service)]
