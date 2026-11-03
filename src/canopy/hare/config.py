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
    # Max concurrent describe calls across all subscribes. A describe runs only
    # once a PV's monitor has delivered a connected value, so a channel that
    # never connects takes no slot.
    seed_concurrency: int = 32
    seed_timeout_s: float = 2.0
    """Seconds a newly subscribed PV waits for its monitor's first value before being reported ``connected: false``.

    The monitor delivers the current value as soon as the channel connects, so
    no one-shot read is made and a slow channel still fills in later; this only
    bounds how long subscribers wait to learn that a channel has not answered.
    """
    seed_retry_backoff_s: float = 30.0
    """Seconds before a subscribe retries a describe that failed on a connected channel.

    A channel that reconnects is described again at once regardless; the
    backoff only stops repeated subscribes of a PV whose describe keeps failing
    from retrying it on every subscribe.
    """


def get_hare_config() -> HareConfig:
    return HareConfig()
