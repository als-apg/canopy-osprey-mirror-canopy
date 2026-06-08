"""Configuration for the EPICS connector package.

Standard EPICS network discovery (``EPICS_CA_ADDR_LIST``, ``EPICS_PVA_ADDR_LIST``,
etc.) is configured via the EPICS library's own environment variables, not here.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class EpicsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_EPICS__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # `simulation` (default, synthetic) or `epics` (real CA/PVA), or a
    # `module:Class` dotted path to an out-of-tree backend.
    backend: str = "simulation"

    # --- simulation backend ---
    sim_update_hz: float = 5.0
    sim_default_units: str = ""

    # --- real (epics) backend ---
    ca_timeout_s: float = 5.0
    pva_timeout_s: float = 5.0
    # When a name is unprefixed and CA fails to connect, retry over PVA.
    unprefixed_pva_fallback: bool = False


def get_epics_config() -> EpicsConfig:
    return EpicsConfig()
