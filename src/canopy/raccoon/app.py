import logging

from fastapi import FastAPI

from canopy.core.errors import register_canopy_error_handler
from canopy.raccoon.api.v1.router import router as v1_router

logger = logging.getLogger(__name__)


app = FastAPI(
    title="Canopy / Raccoon",
    description="PV name-lookup proxy over the lab directory service.",
)

register_canopy_error_handler(app)
app.include_router(v1_router)
