from fastapi import APIRouter

from canopy.converter.api.v1.health import router as health_router
from canopy.converter.api.v1.conversions import router as conversions_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(conversions_router)
