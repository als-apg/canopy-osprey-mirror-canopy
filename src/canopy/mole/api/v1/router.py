from fastapi import APIRouter

from canopy.mole.api.v1.data import router as data_router
from canopy.mole.api.v1.health import router as health_router
from canopy.mole.api.v1.search import router as search_router
from canopy.mole.api.v1.metadata import router as metadata_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(data_router)
router.include_router(metadata_router)
router.include_router(search_router)
