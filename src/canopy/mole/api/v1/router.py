from fastapi import APIRouter

from canopy.mole.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
