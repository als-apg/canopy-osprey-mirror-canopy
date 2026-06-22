from fastapi import APIRouter

from canopy.fox.api.v1.ws import router as ws_router
from canopy.fox.api.v1.health import router as health_router
from canopy.fox.api.v1.evaluate import router as evaluate_router
from canopy.fox.api.v1.validate import router as validate_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(evaluate_router)
router.include_router(validate_router)
router.include_router(ws_router)
