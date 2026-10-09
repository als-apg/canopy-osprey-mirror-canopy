"""The simulation backend against the `PVConnector` conformance suite."""

import pytest

from canopy.epics.base import PVWriteFailed
from canopy.contracts.pv import PVWriteValue
from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.epics.testing import conformance
from canopy.epics.backends.simulation import SimulationConnector

_PREFIX = {"bare": "", "ca": "ca://", "pva": "pva://"}
_NAMES = {
    "scalar": "SIM:CONFORMANCE:SCALAR",
    "enum": "SIM:CONFORMANCE:ENUM",
    "waveform": "SIM:CONFORMANCE:WAVEFORM",
    conformance.NEVER: "SIM:NEVER:CONFORMANCE",
}


class SimulationDriver:
    """The simulated control system lives inside the connector, so the driver
    builds the connector itself and sets values on the one it built last."""

    def __init__(self) -> None:
        self._connector: SimulationConnector | None = None
        self._values: dict[str, PVWriteValue] = {}

    def make_connector(self) -> SimulationConnector:
        connector = build_connector(EpicsConfig(backend="simulation", sim_update_hz=50.0))
        connector._overrides.update(self._values)
        self._connector = connector
        return connector

    def pv(self, kind: str, form: str) -> str:
        return _PREFIX[form] + _NAMES[kind]

    async def set_value(self, pv: str, value: PVWriteValue) -> None:
        # The connector holds an override under the name without its protocol prefix.
        name = strip_protocol_prefix(pv)
        self._values[name] = value
        if self._connector is not None:
            self._connector._overrides[name] = value


@pytest.mark.parametrize("case_id", conformance.case_ids())
async def test_conformance(case_id):
    driver = SimulationDriver()
    try:
        await conformance.run_case(case_id, driver.make_connector, driver)
    except conformance.CaseSkipped as skip:
        pytest.skip(str(skip))


@pytest.mark.parametrize("pv", ["SIM:NEVER:X", "ca://SIM:NEVER:X", "pva://SIM:NEVER:X:ENUM"])
async def test_never_connecting_name_reads_disconnected_and_refuses_writes(pv):
    conn = SimulationConnector.from_config(EpicsConfig())
    value = await conn.get(pv)
    assert value.connected is False
    assert value.value is None
    assert value.pv == pv
    with pytest.raises(PVWriteFailed):
        await conn.put(pv, 1.0)
    assert (await conn.get(pv)).connected is False


async def test_never_prefix_elsewhere_in_the_name_is_an_ordinary_pv():
    conn = SimulationConnector.from_config(EpicsConfig())
    assert (await conn.get("X:SIM:NEVER:Y")).connected is True
