import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from canopy.fox.app import app as fox_app
from canopy.fox.app import fox_lifespan
from canopy.hare.app import app as hare_app
from canopy.hare.app import hare_lifespan
from canopy.jobs.app import app as jobs_app
from canopy.jobs.app import jobs_lifespan
from canopy.mole.app import app as mole_app
from canopy.mole.app import mole_lifespan
from canopy.vole.app import app as vole_app
from canopy.vole.app import vole_lifespan
from canopy.beaver.app import app as beaver_app
from canopy.beaver.app import beaver_lifespan
from canopy.core.errors import register_canopy_error_handler
from canopy.raccoon.app import app as raccoon_app
from canopy.raccoon.app import raccoon_lifespan
from canopy.core.logging import configure_logging

try:
    __version__ = version("canopy")
except PackageNotFoundError:
    __version__ = "0.0.0"


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Compose per-module lifespans. Mounted sub-apps don't run their own."""
    async with (
        mole_lifespan(),
        hare_lifespan(),
        raccoon_lifespan(),
        vole_lifespan(),
        fox_lifespan(),
        beaver_lifespan(),
        jobs_lifespan(),
    ):
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
app.mount("/hare", hare_app)
app.mount("/raccoon", raccoon_app)
app.mount("/vole", vole_app)
app.mount("/fox", fox_app)
app.mount("/beaver", beaver_app)
app.mount("/jobs", jobs_app)


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
