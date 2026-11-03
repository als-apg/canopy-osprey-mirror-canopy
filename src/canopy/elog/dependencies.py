"""Typed ``Annotated[..., Depends(...)]`` aliases for the e-log API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.elog import gateway
from canopy.elog.services.elog_service import ElogService


def get_elog_service() -> ElogService:
    return gateway.get_service()


ElogServiceDep = Annotated[ElogService, Depends(get_elog_service)]
