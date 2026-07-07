"""GET /beaver/v1/screen-schema[/{version}] - serve the canonical Screen IR JSON Schema.

The schema is vendored byte-for-byte from the ``@canopy/screen-ir`` package
(see ``canopy.beaver.services.screen_schema_service``) and returned as raw
bytes, so clients receive a document byte-identical to the npm-published one
rather than a re-serialized copy. Two endpoints:

- ``GET /beaver/v1/screen-schema`` lists the vendored schema versions.
- ``GET /beaver/v1/screen-schema/{version}`` returns the raw schema document
  for that version. Unknown versions produce a 404.
"""

from typing import Annotated

from fastapi import Path, Security, APIRouter
from fastapi.responses import Response

from canopy.core.security import require_read_access
from canopy.beaver.schemas.screen_schema import ScreenSchemaVersionListDTO
from canopy.beaver.services.screen_schema_service import list_versions, get_schema_bytes

router = APIRouter(prefix="/screen-schema", tags=["Beaver / Screen Schema"])


@router.get("", dependencies=[Security(require_read_access)])
def list_schema_versions() -> ScreenSchemaVersionListDTO:
    # Sync handler: touches the filesystem; Starlette threadpools `def` routes.
    versions = list_versions()
    return ScreenSchemaVersionListDTO(versions=versions, total=len(versions))


@router.get("/{version}", dependencies=[Security(require_read_access)])
def get_schema(
    version: Annotated[str, Path(description="Screen IR schema version (e.g. 1.0)")],
) -> Response:
    # Sync handler: touches the filesystem; Starlette threadpools `def` routes.
    return Response(content=get_schema_bytes(version), media_type="application/json")
