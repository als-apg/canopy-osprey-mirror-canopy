from fastapi import APIRouter

from canopy.beaver.api.v1.icons import router as icons_router
from canopy.beaver.api.v1.health import router as health_router
from canopy.beaver.api.v1.screens import router as screens_router
from canopy.beaver.api.v1.widgets import router as widgets_router
from canopy.beaver.api.v1.screen_schema import router as screen_schema_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(widgets_router)
router.include_router(icons_router)
router.include_router(screens_router)
router.include_router(screen_schema_router)
