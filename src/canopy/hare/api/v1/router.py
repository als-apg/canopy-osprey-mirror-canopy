from fastapi import APIRouter

from canopy.hare.api.v1.ws import router as ws_router
from canopy.hare.api.v1.pvs import router as pvs_router
from canopy.hare.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(pvs_router)
router.include_router(ws_router)
