from pydantic_settings import BaseSettings, SettingsConfigDict


class VoleConfig(BaseSettings):
    """Vole's configuration.

    Sourced from env vars with prefix ``CANOPY_VOLE__`` and nested delimiter
    ``__``.  See ``.env.example`` for all options.
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_VOLE__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    max_age_ns: int = 3_600_000_000_000
    max_points: int = 1_000_000
    global_bytes_max: int = 4_294_967_296
    default_align: str = "last"
    default_index: str = "union"


def get_vole_config() -> VoleConfig:
    return VoleConfig()
