from typing import Annotated

from fastapi import Depends

from canopy.vole import gateway
from canopy.vole.services.buffer_service import BufferService


def get_buffer_service() -> BufferService:
    return gateway.get_service()


BufferServiceDep = Annotated[BufferService, Depends(get_buffer_service)]
