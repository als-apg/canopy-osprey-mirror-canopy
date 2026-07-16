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

    # --- resource caps (protect the shared broker from one abusive client) --- #
    # Max PV names accepted in a single subscribe frame (excess is dropped).
    max_pvs_per_message: int = 5_000
    # Max distinct PVs a single client may be subscribed to at once.
    max_subscriptions_per_client: int = 20_000
    # Bound on the internal event queue so a fast producer can't grow it without
    # limit (OOM). At capacity the oldest-losing policy is "drop newest + warn".
    event_queue_maxsize: int = 100_000
    # Max concurrent seed (get+describe) calls when activating a subscribe batch.
    seed_concurrency: int = 32


def get_hare_config() -> HareConfig:
    return HareConfig()
