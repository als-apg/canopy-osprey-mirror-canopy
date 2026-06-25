"""Legacy ``.ui`` / ``.py`` serving for the PyQt compatibility bridge.

Beaver does not parse these files; it serves bytes. Two endpoints:

- ``GET /beaver/v1/screens`` lists every ``.ui`` / ``.py`` file under
  ``BeaverConfig.legacy_screens_dir`` with size and modification time.
- ``GET /beaver/v1/screens/{file_path:path}`` returns the file with the
  appropriate ``Content-Type``. ``file_path`` is resolved against the base
  and rejected if it escapes (symlink or ``..``) or has an unrecognized
  suffix.
"""

from typing import Annotated
from pathlib import Path

from fastapi import Path as PathParam
from fastapi import Security, APIRouter
from fastapi.responses import FileResponse

from canopy.beaver.errors import LegacyScreenNotFound
from canopy.core.security import require_read_access
from canopy.beaver.dependencies import WidgetServiceDep
from canopy.beaver.schemas.legacy import LegacyScreenListDTO, LegacyScreenSummaryDTO

router = APIRouter(prefix="/screens", tags=["Beaver / Screens"])


@router.get("", dependencies=[Security(require_read_access)])
def list_legacy_screens(service: WidgetServiceDep) -> LegacyScreenListDTO:
    # Sync handler on purpose: rglob/stat block, and legacy screen trees live
    # on NFS in practice. Starlette runs `def` routes in its threadpool.
    base = service.config.legacy_screens_dir
    content_types = service.config.content_types
    if not base.is_dir():
        return LegacyScreenListDTO(screens=[], total=0)
    summaries: list[LegacyScreenSummaryDTO] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in content_types:
            continue
        stat = path.stat()
        summaries.append(
            LegacyScreenSummaryDTO(
                path=path.relative_to(base).as_posix(),
                size_bytes=stat.st_size,
                modified_ts_ns=stat.st_mtime_ns,
            )
        )
    return LegacyScreenListDTO(screens=summaries, total=len(summaries))


@router.get("/{file_path:path}", dependencies=[Security(require_read_access)])
def get_legacy_screen(
    file_path: Annotated[str, PathParam(description="Relative path under legacy_screens_dir")],
    service: WidgetServiceDep,
) -> FileResponse:
    base = service.config.legacy_screens_dir
    content_types = service.config.content_types
    resolved = (base / file_path).resolve()
    base_resolved = base.resolve()
    if not _is_within(resolved, base_resolved) or not resolved.is_file():
        raise LegacyScreenNotFound(f"Legacy screen not found: {file_path}")
    if resolved.suffix not in content_types:
        raise LegacyScreenNotFound(f"Legacy screen not found: {file_path}")
    return FileResponse(resolved, media_type=content_types[resolved.suffix])


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
