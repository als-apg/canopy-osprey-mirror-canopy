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

    database_url: str = "postgresql+asyncpg://canopy:canopy@localhost:5432/canopy"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis — Arq job queue + per-job progress; used by canopy-worker and the jobs module.
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
