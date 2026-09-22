from typing import Literal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    database_url: str = "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis — Arq job queue + per-job progress; used by canopy-worker and the jobs module.
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
