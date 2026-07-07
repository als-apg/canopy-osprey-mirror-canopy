from fastapi import APIRouter

from canopy.squirrel.api.v1.pvs import router as pvs_router
from canopy.squirrel.api.v1.tags import router as tags_router
from canopy.squirrel.api.v1.health import router as health_router
from canopy.squirrel.api.v1.snapshots import router as snapshots_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(snapshots_router)
router.include_router(pvs_router)
router.include_router(tags_router)
