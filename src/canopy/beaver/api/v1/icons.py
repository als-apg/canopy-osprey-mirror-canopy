"""GET /beaver/v1/icons/{filename} - serve SVG widget palette icons.

Reads from ``BeaverConfig.icons_dir``, validates the filename against a strict
pattern (no path separators or leading dots) to prevent traversal, and returns
the SVG with a long Cache-Control max-age suitable for an immutable asset.
"""

import re
from typing import Annotated

from fastapi import Path, Security, APIRouter
from fastapi.responses import FileResponse

from canopy.beaver.errors import IconNotFound
from canopy.core.security import require_read_access
from canopy.beaver.dependencies import WidgetServiceDep

router = APIRouter(prefix="/icons", tags=["Beaver / Icons"])

# Filename must start with alphanumeric and contain only alphanumerics, dot,
# underscore, dash. Blocks "..", "/", "\", and any leading-dot dotfiles.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@router.get("/{filename}", dependencies=[Security(require_read_access)])
def get_icon(
    filename: Annotated[str, Path(description="SVG icon filename")],
    service: WidgetServiceDep,
) -> FileResponse:
    # Sync handler: is_file() touches the filesystem; Starlette threadpools `def` routes.
    if not _SAFE_NAME.match(filename):
        raise IconNotFound(f"Invalid icon filename: {filename!r}")
    config = service.config
    path = config.icons_dir / filename
    if not path.is_file():
        raise IconNotFound(f"Icon not found: {filename}")
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={"Cache-Control": f"public, max-age={config.icon_cache_max_age_s}"},
    )
