from fastapi import APIRouter

from canopy.magpie.api.v1.health import router as health_router
from canopy.magpie.api.v1.configs import router as configs_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(configs_router)
