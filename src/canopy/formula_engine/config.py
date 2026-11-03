import os
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

LEGACY_ENV_PREFIX = "CANOPY_FOX__"
"""The Formula Engine's prefix while it was named Fox. Nothing reads it any more."""


class FormulaEngineConfig(BaseSettings):
    """The Formula Engine's configuration.

    Sourced from env vars with prefix ``CANOPY_FORMULA_ENGINE__`` and nested delimiter ``__``.
    Examples::

        CANOPY_FORMULA_ENGINE__EVAL_TIMEOUT_MS=250
        CANOPY_FORMULA_ENGINE__MAX_OUTPUT_POINTS=10000
        CANOPY_FORMULA_ENGINE__MAX_EXPRESSION_LEN=2048
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_FORMULA_ENGINE__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    eval_timeout_ms: int = 250
    max_output_points: int = 10_000
    max_expression_len: int = 2048

    # --- resource caps --- #
    # Floor on poll-mode interval so a client can't request a tight re-eval loop
    # (e.g. intervalMs=1 -> ~1000 evals/sec) that pins a CPU.
    min_interval_ms: int = 50
    # Max live formula subscriptions a single client may hold at once.
    max_subscriptions_per_client: int = 1_000
    # Max columns in one ``table`` spec; its bindings may number one more (a
    # trigger PV that is not plotted). Above either, the spec is ``invalid_spec``.
    table_max_columns: int = 16
    # Live table subscriptions one client may hold; the next one gets ``table_limit``.
    table_max_per_client: int = 8
    # Table row stores kept after their last listener left (for a quick
    # re-subscribe); above this the oldest lingering store is dropped.
    table_max_lingering: int = 64

    # --- live tables --- #
    # A table row is final this long (receive clock) after it arrived, or once
    # every input has moved past it; newer rows are a provisional tail.
    table_settle_horizon_ms: int = 2000
    # Poll period of a Trigger PV table, and the floor of a Timer table's Δt.
    table_poll_ms: int = 500
    # How long an ended table's rows linger for a re-subscribe, the delay before
    # a failed history seed is retried, and how long ``lateAfterFinal`` stays set.
    table_linger_s: float = 60.0
    table_retry_s: float = 60.0
    table_late_window_s: float = 60.0

    # --- live subscriptions --- #
    # A live formula whose inputs have no data yet is retried server-side. Once
    # that wait lasts this long, the subscriber gets one error frame flagged
    # ``retryable`` (a result later supersedes it). Shorter waits stay silent.
    input_wait_notify_s: float = 1.0


def get_formula_engine_config() -> FormulaEngineConfig:
    return FormulaEngineConfig()


def warn_legacy_env() -> None:
    """Warn once if any ``CANOPY_FOX__*`` variable is still set.

    Those variables are ignored since the rename, so a deployment that still
    sets them silently runs on the defaults. Called from the module's lifespan,
    which runs once per process.
    """
    legacy = sorted(name for name in os.environ if name.startswith(LEGACY_ENV_PREFIX))
    if legacy:
        logger.warning(
            "Ignoring %s: the Formula Engine reads %s* now, so these settings fall back to their defaults",
            ", ".join(legacy),
            FormulaEngineConfig.model_config["env_prefix"],
        )
