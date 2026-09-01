from typing import Annotated

from fastapi import Depends

from canopy.vole import gateway
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.vole.services.buffer_service import BufferService


def get_buffer_service() -> BufferService:
    return gateway.get_service()


def get_ingest_service() -> HareIngestService:
    return gateway.get_ingest()


BufferServiceDep = Annotated[BufferService, Depends(get_buffer_service)]
IngestServiceDep = Annotated[HareIngestService, Depends(get_ingest_service)]
