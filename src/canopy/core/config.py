from typing import Literal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core application settings, read from the environment with the ``CANOPY_`` prefix.

    Attributes
    ----------
    debug : bool
        Include verbose error detail in responses.
    api_keys : str
        API-key registry string, ``"<token>:<scope>[+<scope>][,...]"``.
    log_format : {"text", "json"}
        Line format for every log handler; ``"json"`` is what the Promtail scraper parses.
    log_path : str | None
        Optional file sink alongside stdout, rotated by size; ``None`` means stdout only.
    log_level : str
        Root log level name for the API and worker processes, matched case-insensitively.
        An unknown name falls back to ``INFO`` with a warning.
    database_url : str
        Postgres DSN using the asyncpg driver.
    database_pool_size : int
        Connections kept in the SQLAlchemy pool.
    database_max_overflow : int
        Connections the pool may open beyond ``database_pool_size``.
    redis_url : str
        Redis DSN backing the Arq job queue and per-job progress.
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_",
        env_file=".env",
        extra="ignore",
    )

    debug: bool = False
    api_keys: str = ""

    # Logging — shared by canopy-api and canopy-worker (both call
    # core.logging.configure_logging at import time). "json" emits one JSON
    # object per line, which the Promtail scraper in docker/observability/
    # parses without regex extraction. log_path adds a file handler alongside
    # stdout so Promtail can tail a file; unset means stdout only.
    log_format: Literal["text", "json"] = "text"
    log_path: str | None = None
    log_level: str = "INFO"

    # API key cache. `api_key_cache_ttl_s` is also the revocation latency: a key
    # deactivated in the database keeps working until its cached entry expires.
    # `api_key_max_stale_s` bounds how long a cached key may be served while
    # Postgres is unreachable, so an outage can't keep a revoked key alive forever.
    api_key_cache_ttl_s: float = 60.0
    api_key_negative_cache_ttl_s: float = 5.0
    api_key_max_stale_s: float = 3600.0

    database_url: str = "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis — Arq job queue + per-job progress; used by canopy-worker and the jobs module.
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
