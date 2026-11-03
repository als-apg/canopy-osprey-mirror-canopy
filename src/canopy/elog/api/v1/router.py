from fastapi import APIRouter

from canopy.elog.api.v1.apps import router as apps_router
from canopy.elog.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(apps_router)
router.include_router(health_router)
