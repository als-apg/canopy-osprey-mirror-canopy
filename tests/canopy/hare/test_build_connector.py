"""build_connector factory: routes config.backend (name or module:Class path) to a connector, errors on unknown."""

import sys
import types

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
    with pytest.raises(EpicsBackendUnavailable, match="module:Class"):
        build_connector(EpicsConfig(backend="bogus"))


class _OutOfTreeConnector:
    backend_name = "out-of-tree"

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg) -> "_OutOfTreeConnector":
        return cls(cfg)


@pytest.fixture
def backend_module(monkeypatch):
    """An importable module holding one usable backend class and two unusable ones."""
    name = "canopy_test_out_of_tree_backend"
    module = types.ModuleType(name)
    module.Connector = _OutOfTreeConnector
    module.NoFromConfig = type("NoFromConfig", (), {"backend_name": "broken"})
    module.FromConfigIsData = type("FromConfigIsData", (), {"from_config": "not callable"})
    monkeypatch.setitem(sys.modules, name, module)
    return name


def test_module_class_path_is_built_from_config(backend_module):
    cfg = EpicsConfig(backend=f"{backend_module}:Connector")
    conn = build_connector(cfg)
    assert isinstance(conn, _OutOfTreeConnector)
    assert conn.cfg is cfg


def test_module_class_path_reaches_a_shipped_backend():
    conn = build_connector(EpicsConfig(backend="canopy.epics.backends.simulation:SimulationConnector"))
    assert isinstance(conn, SimulationConnector)


def test_backend_path_is_read_from_the_environment(backend_module, monkeypatch):
    monkeypatch.setenv("CANOPY_EPICS__BACKEND", f"{backend_module}:Connector")
    assert isinstance(build_connector(EpicsConfig()), _OutOfTreeConnector)


def test_missing_module_raises_with_the_import_error_chained():
    with pytest.raises(EpicsBackendUnavailable, match="canopy_no_such_module.backend") as err:
        build_connector(EpicsConfig(backend="canopy_no_such_module.backend:Connector"))
    assert isinstance(err.value.__cause__, ImportError)


def test_module_whose_own_import_fails_raises(monkeypatch, tmp_path):
    (tmp_path / "canopy_test_broken_backend.py").write_text("import canopy_no_such_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend="canopy_test_broken_backend:Connector"))
    assert "canopy_test_broken_backend" in str(err.value)
    assert "canopy_no_such_dependency" in str(err.value)


def test_missing_class_raises(backend_module):
    with pytest.raises(EpicsBackendUnavailable, match="not found in") as err:
        build_connector(EpicsConfig(backend=f"{backend_module}:Absent"))
    assert "Absent" in str(err.value)
    assert backend_module in str(err.value)


@pytest.mark.parametrize("class_name", ["NoFromConfig", "FromConfigIsData"])
def test_class_without_a_callable_from_config_raises(backend_module, class_name):
    path = f"{backend_module}:{class_name}"
    with pytest.raises(EpicsBackendUnavailable, match="has no callable 'from_config' classmethod") as err:
        build_connector(EpicsConfig(backend=path))
    assert path in str(err.value)


@pytest.mark.parametrize("path", [":Connector", "canopy.epics.gateway:", ".gateway:Connector"])
def test_malformed_path_raises(path):
    with pytest.raises(EpicsBackendUnavailable, match="is not a 'module.path:ClassName' dotted path") as err:
        build_connector(EpicsConfig(backend=path))
    assert repr(path) in str(err.value)
