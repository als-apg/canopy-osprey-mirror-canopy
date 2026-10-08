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
    # How often a Channel Access channel whose server went away is dropped and
    # searched for from scratch. libca's own search for a lost channel slows to
    # one attempt every few minutes, so without this a PV stays disconnected long
    # after its server is back. A read also drops a lost channel, at most once per
    # `ca_timeout_s`. 0 leaves lost channels to libca.
    ca_reconnect_s: float = 15.0
    # Treat a CA DBR_CHAR array as a string rather than a numeric waveform.
    char_array_as_string: bool = True

    # How long a monitor waits for its channel before reporting it not connected.
    # A PV that has not connected by then yields one `connected=False` value, so a
    # client can tell "no such PV" from "still waiting". It updates normally if it
    # connects later. Applies to every backend.
    connect_report_s: float = 1.5

    # Max waveform length we will serialize as JSON and fan out to WebSocket clients.
    # Longer arrays are truncated and flagged via PVValue.error. This is a downstream
    # serialization/display guard applied after the array is already in hand, and is distinct
    # from EPICS_CA_MAX_ARRAY_BYTES.
    max_array_elements: int = 100_000


def get_epics_config() -> EpicsConfig:
    return EpicsConfig()
