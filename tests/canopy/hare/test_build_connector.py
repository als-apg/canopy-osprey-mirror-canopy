"""build_connector factory: routes config.backend to a connector, errors on unknown."""

import pytest

from canopy.epics.base import EpicsBackendUnavailable
from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector
from canopy.epics.backends.simulation import SimulationConnector


def test_default_backend_is_simulation():
    conn = build_connector(EpicsConfig())
    assert isinstance(conn, SimulationConnector)
    assert conn.backend_name == "simulation"


def test_epics_backend_selected_by_name():
    conn = build_connector(EpicsConfig(backend="epics"))
    assert conn.backend_name == "epics"


def test_unknown_backend_raises():
    with pytest.raises(EpicsBackendUnavailable):
        build_connector(EpicsConfig(backend="bogus"))
