from fastapi import APIRouter

from canopy.analytics.api.v1.events import router as events_router
from canopy.analytics.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(events_router)
router.include_router(health_router)
