from fastapi import APIRouter

from canopy.jobs.api.v1.jobs import router as jobs_router
from canopy.jobs.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(jobs_router)
