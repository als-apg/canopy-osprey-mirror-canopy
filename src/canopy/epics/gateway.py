"""Public surface of the EPICS connector package.

Hare imports `build_connector` and `PVConnector` from here; the rest of
`canopy.epics.*` is private implementation.
"""

import importlib

from canopy.epics.base import PVConnector, EpicsBackendUnavailable
from canopy.epics.config import EpicsConfig


def build_connector(config: EpicsConfig) -> PVConnector:
    """Construct the connector backend named by `config.backend`.

    Three forms are supported:
      - ``"simulation"`` -- the in-process synthetic backend (the default).
      - ``"epics"`` -- real Channel Access / PV Access.
      - ``"module.path:ClassName"`` -- dynamic import of an out-of-tree backend.
    """
    backend = config.backend
    if backend == "simulation":
        from canopy.epics.backends.simulation import SimulationConnector

        return SimulationConnector.from_config(config)
    if backend == "epics":
        from canopy.epics.backends.epics_backend import EpicsConnector

        return EpicsConnector.from_config(config)
    if ":" in backend:
        return _resolve_backend_class(backend).from_config(config)
    raise EpicsBackendUnavailable(
        f"Unknown EPICS backend {backend!r}. Valid backends: 'simulation', 'epics', or a 'module:Class' dotted path."
    )


def _resolve_backend_class(path: str) -> type[PVConnector]:
    """Import the class a ``module.path:ClassName`` backend string names."""
    module_path, _, class_name = path.partition(":")
    try:
        module = importlib.import_module(module_path)
    except (ImportError, ValueError) as exc:
        raise EpicsBackendUnavailable(f"Cannot import module '{module_path}' of EPICS backend {path!r}: {exc}") from exc
    cls = getattr(module, class_name, None)
    if cls is None:
        raise EpicsBackendUnavailable(f"Class '{class_name}' of EPICS backend {path!r} not found in '{module_path}'")
    if not callable(getattr(cls, "from_config", None)):
        raise EpicsBackendUnavailable(f"EPICS backend {path!r} has no 'from_config' classmethod")
    return cls
