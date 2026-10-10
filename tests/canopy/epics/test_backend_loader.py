"""`build_connector` backend loading: the built-in names and ``module:Class`` paths."""

import sys
import types

import pytest

from canopy.epics.base import EpicsBackendUnavailable
from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector
from canopy.epics.backends.simulation import SimulationConnector


class _OutOfTreeConnector:
    backend_name = "out-of-tree"

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg) -> "_OutOfTreeConnector":
        return cls(cfg)


class _NoFromConfig:
    backend_name = "broken"


@pytest.fixture
def backend_module(monkeypatch):
    """An importable module holding one usable backend class and one that is not."""
    name = "canopy_test_out_of_tree_backend"
    module = types.ModuleType(name)
    module.Connector = _OutOfTreeConnector
    module.NoFromConfig = _NoFromConfig
    module.from_config_is_data = type("FromConfigIsData", (), {"from_config": "not callable"})
    monkeypatch.setitem(sys.modules, name, module)
    return name


def test_module_class_path_is_imported_and_built_from_config(backend_module):
    cfg = EpicsConfig(backend=f"{backend_module}:Connector")
    conn = build_connector(cfg)
    assert isinstance(conn, _OutOfTreeConnector)
    assert conn.cfg is cfg


def test_module_class_path_reaches_a_shipped_backend():
    conn = build_connector(EpicsConfig(backend="canopy.epics.backends.simulation:SimulationConnector"))
    assert isinstance(conn, SimulationConnector)


def test_module_that_does_not_import_is_named():
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend="canopy_no_such_module.backend:Connector"))
    assert "canopy_no_such_module.backend" in str(err.value)
    assert isinstance(err.value.__cause__, ImportError)


def test_module_whose_own_import_fails_is_named(monkeypatch, tmp_path):
    (tmp_path / "canopy_test_broken_backend.py").write_text("import canopy_no_such_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend="canopy_test_broken_backend:Connector"))
    assert "canopy_test_broken_backend" in str(err.value)
    assert "canopy_no_such_dependency" in str(err.value)


def test_missing_class_is_named(backend_module):
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend=f"{backend_module}:Absent"))
    assert "Absent" in str(err.value)
    assert backend_module in str(err.value)


@pytest.mark.parametrize("class_name", ["NoFromConfig", "from_config_is_data"])
def test_class_without_from_config_is_named(backend_module, class_name):
    path = f"{backend_module}:{class_name}"
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend=path))
    assert path in str(err.value)
    assert "from_config" in str(err.value)


@pytest.mark.parametrize("path", [":Connector", "canopy.epics.gateway:"])
def test_half_empty_path_raises_the_domain_error(path):
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend=path))
    assert path in str(err.value)


def test_unknown_short_name_points_at_the_path_form():
    with pytest.raises(EpicsBackendUnavailable) as err:
        build_connector(EpicsConfig(backend="bogus"))
    assert "bogus" in str(err.value)
    assert "module:Class" in str(err.value)


def test_backend_path_comes_from_the_environment(backend_module, monkeypatch):
    monkeypatch.setenv("CANOPY_EPICS__BACKEND", f"{backend_module}:Connector")
    assert isinstance(build_connector(EpicsConfig()), _OutOfTreeConnector)


def test_connect_report_bound_defaults_and_reaches_the_epics_backend(monkeypatch):
    assert EpicsConfig().connect_report_s == 1.5
    monkeypatch.setenv("CANOPY_EPICS__CONNECT_REPORT_S", "0.25")
    conn = build_connector(EpicsConfig(backend="epics"))
    assert conn._connect_report == 0.25
