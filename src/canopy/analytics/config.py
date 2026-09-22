"""Analytics configuration.

Sourced from env vars with prefix ``CANOPY_ANALYTICS__``::

    CANOPY_ANALYTICS__ENABLED=false
    CANOPY_ANALYTICS__MAX_EVENT_BYTES=65536
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

    # Reject event bodies larger than this (bytes) with 413. Ingestion is
    # unauthenticated and every accepted event becomes one log line, so this is
    # the only thing between a caller and the log volume. Sized for a
    # frontend_error event carrying a full stack trace, and kept under Loki's
    # default 256 KiB line cap so an accepted event is always queryable.
    max_event_bytes: int = 64 * 1024


def get_analytics_config() -> AnalyticsConfig:
    return AnalyticsConfig()
