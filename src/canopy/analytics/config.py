"""Analytics configuration.

Sourced from env vars with prefix ``CANOPY_ANALYTICS__``::

    CANOPY_ANALYTICS__ENABLED=false
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_ANALYTICS__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # Kill switch: when off, events are accepted (2xx) but not recorded, so a
    # deploy can disable collection without breaking frontend clients.
    enabled: bool = True


def get_analytics_config() -> AnalyticsConfig:
    return AnalyticsConfig()
