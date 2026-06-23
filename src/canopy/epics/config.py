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
    # Element count for simulated waveform PVs (names ending in :WAVEFORM / :WF).
    sim_waveform_length: int = 64

    # --- real (epics) backend ---
    ca_timeout_s: float = 5.0
    pva_timeout_s: float = 5.0
    # When a name is unprefixed and CA fails to connect, retry over PVA.
    unprefixed_pva_fallback: bool = False
    # Treat a CA DBR_CHAR array as a string rather than a numeric waveform.
    char_array_as_string: bool = True

    # Max waveform length we will serialize as JSON and fan out to WebSocket clients.
    # Longer arrays are truncated and flagged via PVValue.error. This is a downstream
    # serialization/display guard applied after the array is already in hand, and is distinct
    # from EPICS_CA_MAX_ARRAY_BYTES.
    max_array_elements: int = 100_000


def get_epics_config() -> EpicsConfig:
    return EpicsConfig()
