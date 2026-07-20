from fastapi import APIRouter

from canopy.vole.api.v1.pvs import router as pvs_router
from canopy.vole.api.v1.debug import router as debug_router
from canopy.vole.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(debug_router)
router.include_router(pvs_router)
