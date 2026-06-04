"""Public surface of the EPICS connector package.

Hare imports `build_connector` and `PVConnector` from here; the rest of
`canopy.epics.*` is private implementation.
"""

from canopy.epics.base import PVConnector, EpicsBackendUnavailable
from canopy.epics.config import EpicsConfig


def build_connector(config: EpicsConfig) -> PVConnector:
    """Construct the connector backend named by `config.backend`."""
    if config.backend == "simulation":
        from canopy.epics.backends.simulation import SimulationConnector

        return SimulationConnector.from_config(config)
    if config.backend == "epics":
        from canopy.epics.backends.epics_backend import EpicsConnector

        return EpicsConnector.from_config(config)
    raise EpicsBackendUnavailable(f"Unknown EPICS backend {config.backend!r}. Valid backends: 'simulation', 'epics'.")
