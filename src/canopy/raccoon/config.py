from pydantic_settings import BaseSettings, SettingsConfigDict


class RaccoonConfig(BaseSettings):
    """Raccoon's configuration.

    Sourced from env vars with prefix `CANOPY_RACCOON__` and nested delimiter
    `__`. The `directory` block and cache tunables land in the next slice
    alongside the adapter contract.
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_RACCOON__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )


def get_raccoon_config() -> RaccoonConfig:
    return RaccoonConfig()
