from pydantic_settings import BaseSettings, SettingsConfigDict


class FoxConfig(BaseSettings):
    """Fox's configuration.

    Sourced from env vars with prefix ``CANOPY_FOX__`` and nested delimiter ``__``.
    Examples::

        CANOPY_FOX__EVAL_TIMEOUT_MS=250
        CANOPY_FOX__MAX_OUTPUT_POINTS=10000
        CANOPY_FOX__MAX_EXPRESSION_LEN=2048
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_FOX__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    eval_timeout_ms: int = 250
    max_output_points: int = 10_000
    max_expression_len: int = 2048


def get_fox_config() -> FoxConfig:
    return FoxConfig()
