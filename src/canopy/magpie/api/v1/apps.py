"""App discovery.

``GET /v1/apps`` answers "what apps exist?" — the question the per-app config
routes can't, since they require you to already know an ``app``. An app has no
separate registry; it exists only by having stored configs, so the list is derived
from the table (see ``ConfigService.list_apps``). Cross-app, but returns only names
and counts — no config contents — so it stays a supported resource rather than a
``/_debug`` one.
"""

from fastapi import Security, APIRouter

from canopy.core.security import require_read_access
from canopy.magpie.dependencies import ConfigServiceDep
from canopy.magpie.schemas.config import AppSummaryDTO

router = APIRouter(prefix="/apps", tags=["Apps"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_apps(service: ConfigServiceDep) -> list[AppSummaryDTO]:
    """List every app that has stored configs, with a count of each (alphabetical)."""
    return await service.list_apps()
