import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from canopy.core.logging import configure_logging
from canopy.core.errors import register_canopy_error_handler
from canopy.mole.app import app as mole_app
from canopy.mole.runtime import mark_started as mole_mark_started

try:
    __version__ = version("canopy")
except PackageNotFoundError:
    __version__ = "0.0.0"


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Per-module startup hooks. Mounted sub-apps don't run their own lifespan
    # automatically when behind app.mount(); the parent composes them here.
    mole_mark_started()
    logger.info("Starting Canopy API")
    yield
    logger.info("Canopy API shutdown complete")


app = FastAPI(
    title="Canopy",
    description="Control-system display platform — backend API",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_canopy_error_handler(app)

app.mount("/mole", mole_app)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "name": "Canopy",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }
