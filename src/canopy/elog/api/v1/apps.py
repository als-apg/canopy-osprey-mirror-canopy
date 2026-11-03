"""Per-app e-log routes: ``/elog/v1/apps/{app}/...``.

``{app}`` picks the e-log application token the call is made with; the
API key only decides whether the caller may make it (module ``elog``, plus
``read`` or ``write``).
"""

from typing import Annotated

from fastapi import File, Query, Security, APIRouter, UploadFile

from canopy.elog.errors import ElogAttachmentTooLarge
from canopy.core.security import APIKey, require_read_access, require_write_access
from canopy.elog.dependencies import ElogServiceDep
from canopy.elog.schemas.elog import (
    ElogTagDTO,
    ElogUserDTO,
    ElogConfigDTO,
    ElogLogbookDTO,
    ElogAttachmentDTO,
    ElogEntryCreateDTO,
    ElogEntryResultDTO,
    ElogRecentEntryDTO,
)

router = APIRouter(prefix="/apps/{app}", tags=["Elog / Apps"])


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    # FastAPI has already parsed the whole multipart body by now, spooling the
    # file to a temporary file, and ``size`` is the bytes written there. Checking
    # it first keeps an oversized file from being read into memory; the service
    # checks the bytes again for gateway callers. Nothing here caps the request
    # body itself: the proxy in front of Canopy has to (see docs/elog).
    if file.size is not None and file.size > max_bytes:
        raise ElogAttachmentTooLarge(f"Attachment is {file.size} bytes; limit is {max_bytes} bytes")
    return await file.read()


@router.get("/config")
async def get_app_config(app: str, service: ElogServiceDep) -> ElogConfigDTO:
    """Whether ``app`` can post, and its preselected logbooks. No API key needed.

    Always 200 for a well-formed app name: ``enabled=false`` plus a ``reason``
    when no provider is configured or ``app`` has no token.
    """
    return service.app_config(app)


@router.get("/logbooks", dependencies=[Security(require_read_access)])
async def list_logbooks(app: str, service: ElogServiceDep) -> list[ElogLogbookDTO]:
    """Logbooks this app's token can post to."""
    return await service.list_logbooks(app)


@router.get("/logbooks/{logbook_id}/tags", dependencies=[Security(require_read_access)])
async def list_tags(app: str, logbook_id: str, service: ElogServiceDep) -> list[ElogTagDTO]:
    return await service.list_tags(app, logbook_id)


@router.get("/users", dependencies=[Security(require_read_access)])
async def search_users(
    app: str,
    service: ElogServiceDep,
    search: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ElogUserDTO]:
    """People the e-log knows, by name or email: candidates for ``additionalAuthors``."""
    return await service.search_users(app, search, limit=limit)


@router.get("/recent-entries", dependencies=[Security(require_read_access)])
async def list_recent_entries(
    app: str,
    service: ElogServiceDep,
    logbook_ids: Annotated[list[str], Query(alias="logbookId", min_length=1)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[ElogRecentEntryDTO]:
    """Entries this app's token posted to any of ``logbookId`` (repeatable), newest first."""
    return await service.list_recent_entries(app, logbook_ids, limit=limit)


@router.post("/attachments", dependencies=[Security(require_write_access)])
async def upload_attachment(
    app: str,
    service: ElogServiceDep,
    file: Annotated[UploadFile, File(description="The file to attach")],
) -> ElogAttachmentDTO:
    """Upload one file; pass the returned ``id`` in ``attachmentIds`` when posting the entry."""
    data = await _read_upload(file, service.max_attachment_bytes)
    return await service.upload_attachment(
        app, filename=file.filename or "attachment", content_type=file.content_type, data=data
    )


@router.post("/entries")
async def create_entry(
    app: str,
    entry: ElogEntryCreateDTO,
    service: ElogServiceDep,
    api_key: Annotated[APIKey, Security(require_write_access)],
) -> ElogEntryResultDTO:
    """Post an entry as ``app``, or a follow-up of ``followsUpEntryId`` when that is set.

    ``bodyMarkdown`` is rendered to HTML for the e-log. The author recorded is
    the app's own e-log identity.
    """
    return await service.create_entry(app, entry, requested_by=api_key.name)
