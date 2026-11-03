"""Public surface of the e-log module for in-process callers.

Other Canopy modules import only from this file; the rest of
``canopy.elog.*`` is private implementation. ``bind()`` wires the service
singleton at app startup; ``get_service()`` is exposed for
``dependencies.py``. Tests can re-bind a fake service freely.

A caller posts as one of the configured apps (``app="squirrel"``), so the
entry is credited to that app's e-log token exactly as an HTTP post would be.
"""

from canopy.elog.schemas.elog import (
    ElogConfigDTO,
    ElogAttachmentDTO,
    ElogEntryCreateDTO,
    ElogEntryResultDTO,
)
from canopy.elog.services.elog_service import ElogService

_service: ElogService | None = None


def bind(service: ElogService | None) -> None:
    """Wire (or unwire) the ElogService singleton."""
    global _service
    _service = service


def get_service() -> ElogService:
    if _service is None:
        raise RuntimeError("E-log gateway accessed before app startup bound a service")
    return _service


# --------------------------------------------------------------------------- #
# Public facade: thin wrappers over ElogService. Other modules import these.
# --------------------------------------------------------------------------- #


def app_config(app: str) -> ElogConfigDTO:
    """Whether ``app`` can post (``enabled``), and its default logbooks."""
    return get_service().app_config(app)


async def upload_attachment(app: str, *, filename: str, content_type: str | None, data: bytes) -> ElogAttachmentDTO:
    """Upload a file as ``app``; pass the returned id in ``ElogEntryCreateDTO.attachment_ids``."""
    return await get_service().upload_attachment(app, filename=filename, content_type=content_type, data=data)


async def create_entry(app: str, entry: ElogEntryCreateDTO, *, requested_by: str) -> ElogEntryResultDTO:
    """Post ``entry`` as ``app``. ``requested_by`` names the caller in Canopy's own log line."""
    return await get_service().create_entry(app, entry, requested_by=requested_by)
