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
    # Max PVs being seeded (one get + one describe, run concurrently) at once,
    # across all clients. Each PV's initial value is sent as soon as its seed
    # lands, so a slow or unreachable name holds one slot, not the batch.
    seed_concurrency: int = 64

    # --- caching --- #
    # A name whose seed read came back disconnected (or failed) is not looked up
    # again for this long: a re-subscribe within the TTL gets the cached
    # disconnected value at once instead of waiting out the connector timeout
    # again. The monitor still starts, so a channel that comes up is picked up
    # on its first event. 0 disables the cache.
    negative_cache_ttl_s: float = 30.0
    # After a PV loses its last subscriber its monitor and cached value are kept
    # this long, so switching back to a screen is served from cache instead of
    # re-seeding. Mirrors Vole's buffer_grace_seconds. 0 releases at once.
    monitor_grace_seconds: float = 60.0
    seed_retry_backoff_s: float = 30.0
    """Seconds a subscribe waits before re-seeding a held PV whose last seed left its value or metadata missing.

    Bounds how often an unreachable PV makes a subscribe block on connector
    get/describe timeouts. The default is several times the 5.0 s CA and PVA
    connector timeouts, so repeated subscribes of a dead PV pay that timeout at
    most once per window rather than on nearly every subscribe. Until then the
    subscriber is answered from cache (a disconnected value for a failed get).
    ``negative_cache_ttl_s`` covers the same name after it is released.
    """


def get_hare_config() -> HareConfig:
    return HareConfig()
