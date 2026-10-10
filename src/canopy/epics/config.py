"""Configuration for the EPICS connector package.

Standard EPICS network discovery (``EPICS_CA_ADDR_LIST``, ``EPICS_PVA_ADDR_LIST``,
etc.) is configured via the EPICS library's own environment variables, not here.
"""

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EpicsConfig(BaseSettings):
    """Settings for the EPICS connector package.

    A blank ``CANOPY_EPICS__SIM_PUT_ERROR_RATE=``, ``CANOPY_EPICS__SIM_PUT_FAIL_PVS=``
    or ``CANOPY_EPICS__SIM_SEED=`` leaves that field at its default (the ``.env``
    templates show ``SIM_PUT_FAIL_PVS`` and ``SIM_SEED`` blank). Any other blank
    value is taken literally: a blank numeric field such as
    ``CANOPY_EPICS__SIM_UPDATE_HZ=`` fails validation, and a blank
    ``CANOPY_EPICS__BACKEND=`` stops startup instead of falling back to the
    simulation backend.

    Attributes
    ----------
    sim_put_error_rate : float
        Fraction of simulation-backend ``put`` calls that fail, in ``[0.0, 1.0]``.
        ``0.0`` never fails a write; ``1.0`` fails every one.
    sim_put_fail_pvs : str
        Comma-separated PV names whose simulation-backend ``put`` always fails,
        regardless of ``sim_put_error_rate``. A ``ca://`` / ``pva://`` prefix is
        ignored when matching. Blank (the default) means none.
    sim_seed : int | None
        Seed for the simulation backend's failure RNG, so a sequence of writes
        fails reproducibly. ``None`` seeds from the system entropy source.
    """

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
    sim_put_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    sim_put_fail_pvs: str = ""
    sim_seed: int | None = None

    @field_validator("sim_put_error_rate", "sim_seed", mode="before")
    @classmethod
    def _blank_keeps_default(cls, value: object, info: ValidationInfo) -> object:
        """Read a blank failure-injection value as unset.

        Parameters
        ----------
        value : object
            The raw value, a string when it comes from the environment.
        info : ValidationInfo
            Names the field being validated.

        Returns
        -------
        object
            The field's default when `value` is a blank string, otherwise `value`.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

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
