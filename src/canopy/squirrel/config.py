"""Squirrel module configuration.

Sourced from env vars with prefix ``CANOPY_SQUIRREL__`` (nested delimiter ``__``),
mirroring the other Canopy modules. A fresh instance per ``get_squirrel_config()``
call (no caching) so tests can vary the environment freely.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SquirrelConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_SQUIRREL__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # Selected restore-hook provider (e.g. "magnet_trim"); empty = no hook runs.
    restore_hook: str = ""

    # EPICS batch I/O for snapshot create (reads) / restore (writes) over the
    # canopy.epics connector: in-flight concurrency, progress-chunk size, per-op
    # timeout in seconds (None = connector default / no timeout).
    epics_concurrency: int = 64
    epics_chunk_size: int = 500
    epics_timeout_s: float | None = None


def get_squirrel_config() -> SquirrelConfig:
    return SquirrelConfig()
