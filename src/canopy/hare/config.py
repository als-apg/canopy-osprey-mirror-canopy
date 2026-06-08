"""Hare's configuration.

Sourced from env vars with prefix ``CANOPY_HARE__`` and nested delimiter ``__``.
The EPICS connector has its own settings (``CANOPY_EPICS__``); these are the
broker/WebSocket knobs. Examples::

    CANOPY_HARE__WS_BATCH_INTERVAL_MS=100
    CANOPY_HARE__HEARTBEAT_INTERVAL_S=5
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class HareConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_HARE__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # Diff-stream batching window: buffered updates flush to clients this often.
    ws_batch_interval_ms: int = 100
    # Heartbeat frames keep clients aware the broker is alive.
    heartbeat_interval_s: float = 5.0


def get_hare_config() -> HareConfig:
    return HareConfig()
