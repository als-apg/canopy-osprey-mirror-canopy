import logging
from html import escape
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from canopy import __version__
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
from canopy.magpie.app import app as magpie_app
from canopy.magpie.app import magpie_lifespan
from canopy.core.errors import register_canopy_error_handler
from canopy.raccoon.app import app as raccoon_app
from canopy.raccoon.app import raccoon_lifespan
from canopy.core.logging import configure_logging
from canopy.squirrel.app import app as squirrel_app
from canopy.squirrel.app import squirrel_lifespan
from canopy.analytics.app import app as analytics_app
from canopy.analytics.app import analytics_lifespan
from canopy.converter.app import app as converter_app
from canopy.converter.app import converter_lifespan

configure_logging()
logger = logging.getLogger(__name__)

API_MODULES = (
    ("mole", "Mole", "Archiver data and metadata"),
    ("hare", "Hare", "Live process-variable access"),
    ("raccoon", "Raccoon", "PV directory and device discovery"),
    ("vole", "Vole", "Live PV subscriptions"),
    ("fox", "Fox", "Expression evaluation"),
    ("beaver", "Beaver", "Widgets and screens"),
    ("squirrel", "Squirrel", "PV catalog, tags, and snapshots"),
    ("magpie", "Magpie", "Application configuration"),
    ("jobs", "Jobs", "Background job status"),
    ("converter", "Converter", "Screen conversion"),
    ("analytics", "Analytics", "Frontend event ingestion"),
)


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
        squirrel_lifespan(),
        magpie_lifespan(),
        jobs_lifespan(),
        converter_lifespan(),
        analytics_lifespan(),
    ):
        logger.info(f"Starting Canopy API [version {__version__}]")
        yield
        logger.info("Canopy API shutdown complete")


app = FastAPI(
    title="Canopy",
    description="Control-system display platform — backend API",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
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
app.mount("/squirrel", squirrel_app)
app.mount("/magpie", magpie_app)
app.mount("/jobs", jobs_app)
app.mount("/converter", converter_app)
app.mount("/analytics", analytics_app)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
async def docs_index(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    module_links = "".join(
        ('<li><a href="{href}">{name} API</a><p>{description}</p></li>').format(
            href=escape(f"{root_path}/{path}/docs", quote=True),
            name=escape(name),
            description=escape(description),
        )
        for path, name, description in API_MODULES
    )
    return HTMLResponse(
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Canopy API documentation</title>"
        "<style>"
        "body{font:16px system-ui,sans-serif;line-height:1.5;margin:3rem auto;"
        "max-width:56rem;padding:0 1.5rem;color:#202633}"
        "h1{margin-bottom:.25rem}ul{list-style:none;padding:0}"
        "li{border:1px solid #d0d7de;border-radius:.5rem;margin:1rem 0;padding:1rem}"
        "li p{margin:.25rem 0 0;color:#57606a}"
        "</style></head><body>"
        "<main><h1>Canopy API documentation</h1>"
        "<p>Choose a service to view its API and interactive documentation.</p>"
        f"<ul>{module_links}</ul></main></body></html>"
    )


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "name": "Canopy",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }
