"""Canopy API entrypoint: composes every module's sub-app and lifespan into one FastAPI app."""

import json
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
from canopy.core.config import get_settings
from canopy.core.errors import register_canopy_error_handler
from canopy.raccoon.app import app as raccoon_app
from canopy.raccoon.app import raccoon_lifespan
from canopy.core.logging import configure_logging
from canopy.core.modules import tag_module
from canopy.squirrel.app import app as squirrel_app
from canopy.squirrel.app import squirrel_lifespan
from canopy.analytics.app import app as analytics_app
from canopy.analytics.app import analytics_lifespan
from canopy.converter.app import app as converter_app
from canopy.converter.app import converter_lifespan
from canopy.core.security import bind_key_registry
from canopy.core.keys.routes import router as api_keys_router
from canopy.core.keys.registry import build_key_registry

configure_logging()
logger = logging.getLogger(__name__)

API_MODULES = (
    ("mole", "Mole"),
    ("hare", "Hare"),
    ("raccoon", "Raccoon"),
    ("vole", "Vole"),
    ("fox", "Fox"),
    ("beaver", "Beaver"),
    ("squirrel", "Squirrel"),
    ("magpie", "Magpie"),
    ("jobs", "Jobs"),
    ("converter", "Converter"),
    ("analytics", "Analytics"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Compose per-module lifespans. Mounted sub-apps don't run their own."""
    # Env keys first, then the database: the handful of long-lived credentials in
    # the deployment secret keep working when Postgres is unreachable, including on
    # a cold start when the cache has nothing stale to serve. Parsed before anything
    # can serve, so a bad CANOPY_API_KEYS surfaces in the startup log instead of as
    # an unexplained 401 on the first request.
    settings = get_settings()
    bind_key_registry(build_key_registry(settings))
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
tag_module(app, "core")

app.include_router(api_keys_router)

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

tag_module(mole_app, "mole")
tag_module(hare_app, "hare")
tag_module(raccoon_app, "raccoon")
tag_module(vole_app, "vole")
tag_module(fox_app, "fox")
tag_module(beaver_app, "beaver")
tag_module(squirrel_app, "squirrel")
tag_module(magpie_app, "magpie")
tag_module(jobs_app, "jobs")
tag_module(converter_app, "converter")
tag_module(analytics_app, "analytics")


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
async def docs_ui(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    specs = [
        {"url": f"{root_path}/openapi.json", "name": "Canopy"},
        *[{"url": f"{root_path}/{path}/openapi.json", "name": name} for path, name in API_MODULES],
    ]
    serialized_specs = json.dumps(specs).replace("<", "\\u003c")
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<title>{escape(f"{app.title} - Swagger UI")}</title>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
const ui = SwaggerUIBundle({{
    dom_id: "#swagger-ui",
    urls: {serialized_specs},
    "urls.primaryName": "Canopy",
    presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIStandalonePreset
    ],
    layout: "StandaloneLayout",
}});
</script>
</body>
</html>"""
    )


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "name": "Canopy",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }
