"""Squirrel module configuration.

Sourced from env vars with prefix ``CANOPY_SQUIRREL__`` (nested delimiter ``__``),
mirroring the other Canopy modules. A fresh instance per ``get_squirrel_config()``
call (no caching) so tests can vary the environment freely.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SquirrelConfig(BaseSettings):
    """Squirrel's environment-sourced settings."""

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_SQUIRREL__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    restore_hooks: str = Field(
        default="",
        description=(
            'Comma-separated restore-hook provider keys (e.g. "magnet_trim,write_to_con"), '
            "applied in the order given; empty means no hook runs."
        ),
    )

    # EPICS batch I/O for snapshot create (reads) / restore (writes) over the
    # canopy.epics connector: in-flight concurrency, progress-chunk size, per-op
    # timeout in seconds (None = connector default / no timeout).
    epics_concurrency: int = 64
    epics_chunk_size: int = 500
    epics_timeout_s: float | None = None

    @property
    def restore_hooks_list(self) -> list[str]:
        """Split ``restore_hooks`` into provider keys.

        Returns
        -------
        list[str]
            The comma-separated entries, stripped of surrounding whitespace, with
            empty entries dropped and configured order preserved.
        """
        return [name.strip() for name in self.restore_hooks.split(",") if name.strip()]


def get_squirrel_config() -> SquirrelConfig:
    return SquirrelConfig()
