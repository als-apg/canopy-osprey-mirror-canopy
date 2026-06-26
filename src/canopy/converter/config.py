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

    # Batch (folder) conversion: archive uploads are larger than single screens.
    # Reject archives over this size (bytes) before enqueuing. 100 MiB default.
    max_archive_bytes: int = 100 * 1024 * 1024

    # Cap entries converted per batch so a pathological archive cannot run
    # unbounded. Entries beyond the cap are reported as skipped.
    max_batch_entries: int = 5000

    # TTL (seconds) for the staged archive and the per-screen result keys in
    # Redis. Matches the worker's keep_result so a client can poll then fetch.
    batch_result_ttl_s: int = 3600


def get_converter_config() -> ConverterConfig:
    return ConverterConfig()
