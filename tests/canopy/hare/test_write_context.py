"""A PV write's ``binding`` and ``origin``: the PUT body, the broker, the connectors.

The client may name the connector binding its screen was shown and the screen
that issued the write. `PVConnector.put` takes both as keywords; the shipped
backends accept and ignore them, and the broker passes them on as given.
"""

import pytest

from canopy.hare.config import HareConfig
from canopy.epics.config import EpicsConfig
from canopy.hare.schemas.pv import PVWriteDTO, PVWriteOrigin
from canopy.hare.services.broker import PVBroker
from canopy.epics.backends.simulation import SimulationConnector

ORIGIN = {"screen": "ring/overview", "rev": "r1"}


# --- PVWriteDTO -------------------------------------------------------------- #


def test_dto_binding_and_origin_are_optional():
    dto = PVWriteDTO.model_validate({"value": 1.0})
    assert dto.binding is None and dto.origin is None


def test_dto_parses_binding_and_origin():
    dto = PVWriteDTO.model_validate(
        {"value": 1.0, "binding": "b-7", "origin": {"screen": "ring/overview", "rev": "abc"}}
    )
    assert dto.binding == "b-7"
    assert dto.origin == PVWriteOrigin(screen="ring/overview", rev="abc")


def test_dto_origin_rev_is_optional():
    dto = PVWriteDTO.model_validate({"value": 1.0, "origin": {"screen": "ring/overview"}})
    assert dto.origin is not None and dto.origin.rev is None


def test_dto_origin_needs_a_screen():
    with pytest.raises(ValueError):
        PVWriteDTO.model_validate({"value": 1.0, "origin": {"rev": "abc"}})


# --- PVBroker.put ------------------------------------------------------------ #


def _broker(connector) -> PVBroker:
    return PVBroker(connector, HareConfig(heartbeat_interval_s=100.0))


async def test_broker_passes_binding_and_origin_to_the_connector(fake_connector):
    await _broker(fake_connector).put("SIM:A", 1.0, wait=True, timeout=2.0, binding="b-1", origin=ORIGIN)
    assert fake_connector.puts == [("SIM:A", 1.0, True, 2.0)]
    assert fake_connector.put_contexts == [("b-1", ORIGIN)]


async def test_broker_passes_none_when_the_write_named_neither(fake_connector):
    await _broker(fake_connector).put("SIM:A", 1.0)
    assert fake_connector.put_contexts == [(None, None)]


async def test_origin_reaches_the_connector_as_a_plain_dict(fake_connector):
    class _Mapping(dict):
        pass

    await _broker(fake_connector).put("SIM:A", 1.0, origin=_Mapping(ORIGIN))
    ((_, passed),) = fake_connector.put_contexts
    assert type(passed) is dict
    assert passed == ORIGIN


async def test_the_simulation_backend_ignores_binding_and_origin():
    conn = SimulationConnector.from_config(EpicsConfig())
    await conn.put("SIM:CTX", 4.0, binding="b-1", origin=ORIGIN)
    assert (await conn.get("SIM:CTX")).value == 4.0
