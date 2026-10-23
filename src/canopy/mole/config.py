from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArchiverInstanceConfig(BaseModel):
    """One configured archiver instance.

    `type` is either a registered short name (e.g. `"epics_archiver_appliance"`)
    resolved by the registry, or a `module:Class` dotted path for an out-of-tree
    adapter that's been pip-installed alongside Canopy.
    """

    type: str
    url: str
    api_key_env: str | None = None
    timeout_s: float = 30.0
    extras: dict = {}


class MoleConfig(BaseSettings):
    """Mole's configuration.

    Sourced from env vars with prefix `CANOPY_MOLE__` and nested delimiter `__`.
    Examples::

        CANOPY_MOLE__DEFAULT_ARCHIVER=slac
        CANOPY_MOLE__ARCHIVERS__SLAC__TYPE=epics_archiver_appliance
        CANOPY_MOLE__ARCHIVERS__SLAC__URL=https://archiver.example.org
        CANOPY_MOLE__ARCHIVERS__SLAC__TIMEOUT_S=15.0
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_MOLE__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    default_archiver: str = ""
    archivers: dict[str, ArchiverInstanceConfig] = {}


def get_mole_config() -> MoleConfig:
    return MoleConfig()
