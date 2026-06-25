"""Beaver's configuration.

Sourced from env vars with prefix ``CANOPY_BEAVER__`` and nested delimiter ``__``.
Examples::

    CANOPY_BEAVER__WIDGETS_DIR=/srv/canopy/registry/widgets
    CANOPY_BEAVER__ICON_CACHE_MAX_AGE_S=3600
    CANOPY_BEAVER__CONTENT_TYPES='{".ui": "application/xml", ".bob": "application/xml"}'
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class BeaverConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_BEAVER__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    widgets_dir: Path = Path("widget-registry-data/widgets")
    icons_dir: Path = Path("widget-registry-data/icons")
    legacy_screens_dir: Path = Path("widget-registry-data/legacy-screens")
    icon_cache_max_age_s: int = 3600

    # Suffix -> Content-Type for the legacy-screen byte server; this map also
    # defines which suffixes are listed/served. Defaults cover the PyQt bridge
    # (.ui/.py). Override with the full map (a JSON env value) to serve other
    # formats (e.g. Phoebus .bob) without editing this module.
    content_types: dict[str, str] = {
        ".ui": "application/xml",
        ".py": "text/x-python",
    }


def get_beaver_config() -> BeaverConfig:
    return BeaverConfig()
