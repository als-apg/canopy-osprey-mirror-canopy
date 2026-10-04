from typing import Any, Literal
from functools import lru_cache
from collections.abc import Mapping, Iterable

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every mountable module, in composition order. The order is load-bearing: vole
# uses the hare gateway and fox_lifespan reads the service vole_lifespan binds,
# so a selected subset always keeps this relative order.
ALL_MODULES: tuple[str, ...] = (
    "mole",
    "hare",
    "raccoon",
    "vole",
    "fox",
    "beaver",
    "squirrel",
    "magpie",
    "jobs",
    "converter",
    "analytics",
)

# ASGI scope key under which the composed app publishes its selected modules.
# Sub-apps are shared singletons that cannot see the app they are mounted in,
# so the selection travels with the request instead of living in a global.
MODULES_SCOPE_KEY = "canopy.modules"


def resolve_modules(names: Iterable[str]) -> tuple[str, ...]:
    """Validate module names and return them deduplicated, in composition order.

    An empty selection means every module. An unknown name raises ``ValueError``
    so a typo stops startup instead of silently serving fewer routes.
    """
    wanted = {name.strip() for name in names if name.strip()}
    unknown = sorted(wanted - set(ALL_MODULES))
    if unknown:
        raise ValueError(f"Unknown CANOPY module(s): {', '.join(unknown)}. Known modules: {', '.join(ALL_MODULES)}")
    if not wanted:
        return ALL_MODULES
    return tuple(name for name in ALL_MODULES if name in wanted)


def parse_modules(value: str) -> tuple[str, ...]:
    """Resolve a comma-separated ``CANOPY_MODULES`` value (empty: every module)."""
    return resolve_modules(value.split(","))


def module_selected(scope: Mapping[str, Any], name: str) -> bool:
    """True when ``name`` is mounted in the app serving this ASGI scope.

    A scope that carries no selection (a sub-app served on its own, outside the
    composed app) counts every module as selected.
    """
    selected = scope.get(MODULES_SCOPE_KEY)
    return selected is None or name in selected


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_",
        env_file=".env",
        extra="ignore",
    )

    debug: bool = False
    api_keys: str = ""

    # Path of a file holding the key registry, in the same grammar as api_keys.
    # When set it is the only source: api_keys is ignored, and a file that is
    # missing, unreadable or holds no key stops startup instead of leaving an
    # app that answers with the wrong registry. Empty counts as unset.
    api_keys_file: str | None = None

    # Require a registry key on every route of the composed app except
    # GET /health -- the routes that carry no key dependency of their own
    # (/, the docs and OpenAPI pages, the per-module watchdogs) included.
    closed_meta: bool = False

    # Comma-separated names of the modules to mount (see ALL_MODULES); empty
    # means all of them. Kept a plain string because pydantic-settings would
    # JSON-decode a list-typed field. Modules left out are never imported, so
    # an install without the `data` extra can serve the ones that need no database.
    modules: str = ""

    # Logging — shared by canopy-api and canopy-worker (both call
    # core.logging.configure_logging at import time). "json" emits one JSON
    # object per line, which the Promtail scraper in docker/observability/
    # parses without regex extraction. log_path adds a file handler alongside
    # stdout so Promtail can tail a file; unset means stdout only.
    log_format: Literal["text", "json"] = "text"
    log_path: str | None = None
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis — Arq job queue + per-job progress; used by canopy-worker and the jobs module.
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
