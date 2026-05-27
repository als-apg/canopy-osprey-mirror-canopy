from fastapi import APIRouter

from canopy.raccoon.api.v1.pvs import router as pvs_router
from canopy.raccoon.api.v1.health import router as health_router
from canopy.raccoon.api.v1.devices import router as devices_router
from canopy.raccoon.api.v1.elements import router as elements_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(pvs_router)
router.include_router(devices_router)
router.include_router(elements_router)
