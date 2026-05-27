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


@lru_cache
def get_settings() -> Settings:
    return Settings()
