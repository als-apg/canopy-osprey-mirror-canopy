from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical EDM ``colors.list`` bundled with the module (added in Phase 3).
# The EDM parser resolves colours from this file at parse time.
_DEFAULT_COLORS_LIST = Path(__file__).parent / "data" / "colors.list"


class ConverterConfig(BaseSettings):
    """Converter module configuration.

    Sourced from env vars with prefix ``CANOPY_CONVERTER__`` and nested
    delimiter ``__``. Examples::

        CANOPY_CONVERTER__COLORS_LIST_PATH=/etc/edm/colors.list
        CANOPY_CONVERTER__USE_BEAVER_GATEWAY=false
        CANOPY_CONVERTER__MAX_UPLOAD_BYTES=10485760
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_CONVERTER__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # Path to the EDM ``colors.list`` resolved at parse time. Defaults to the
    # canonical copy bundled under ``converter/data/`` (provisioned in Phase 3).
    colors_list_path: Path = _DEFAULT_COLORS_LIST

    # Seam #1: until Beaver (the widget registry) merges, stay on the vendored
    # snapshot shipped inside the ``pydmconverter`` package.
    use_beaver_gateway: bool = False

    # Reject uploads larger than this (bytes) before parsing. 10 MiB default.
    max_upload_bytes: int = 10 * 1024 * 1024


def get_converter_config() -> ConverterConfig:
    return ConverterConfig()
