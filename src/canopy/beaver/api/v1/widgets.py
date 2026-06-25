from typing import Annotated

from fastapi import Path, Query, Security, APIRouter

from canopy.core.security import require_read_access
from canopy.beaver.dependencies import WidgetServiceDep
from canopy.beaver.schemas.widget import (
    WidgetDTO,
    WidgetListDTO,
    CategoryListDTO,
    ValidateResultDTO,
    ValidateRequestDTO,
)

router = APIRouter(prefix="/widgets", tags=["Beaver / Widgets"])


@router.get("", dependencies=[Security(require_read_access)])
async def list_widgets(
    service: WidgetServiceDep,
    category: Annotated[str | None, Query(description="Filter by category id")] = None,
) -> WidgetListDTO:
    summaries = await service.list_summaries(category)
    return WidgetListDTO(widgets=summaries, total=len(summaries))


@router.get("/categories", dependencies=[Security(require_read_access)])
async def list_categories(service: WidgetServiceDep) -> CategoryListDTO:
    return CategoryListDTO(categories=await service.categories())


@router.get("/by-qt-class/{qt_class}", dependencies=[Security(require_read_access)])
async def by_qt_class(
    qt_class: Annotated[str, Path(description="Qt class name (e.g. PyDMLabel)")],
    service: WidgetServiceDep,
) -> WidgetDTO:
    return await service.by_qt_class(qt_class)


@router.get("/{widget_id}", dependencies=[Security(require_read_access)])
async def get_widget(
    widget_id: Annotated[str, Path(description="kebab-case widget id")],
    service: WidgetServiceDep,
) -> WidgetDTO:
    return await service.get(widget_id)


@router.post("/{widget_id}/validate", dependencies=[Security(require_read_access)])
async def validate_widget(
    widget_id: Annotated[str, Path(description="kebab-case widget id")],
    body: ValidateRequestDTO,
    service: WidgetServiceDep,
) -> ValidateResultDTO:
    return await service.validate_props(widget_id, body.props)
