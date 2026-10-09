"""Unit tests for the PV identity rule behind the duplicate checks (no DB)."""

import pytest

from canopy.core.errors import BadRequest
from canopy.squirrel.schemas.pv import NewPVElementDTO
from canopy.squirrel.services.pv_service import PvService, _identity


def _service() -> PvService:
    """A service whose repositories are never queried, as the in-import checks run first."""
    return PvService(session=None)


def test_setpoint_is_the_identity_when_present():
    assert _identity("DEV:SP", "DEV:RB", "DEV:CFG") == ("setpoint", "DEV:SP")


def test_readback_is_the_identity_without_a_setpoint():
    assert _identity(None, "DEV:RB", "DEV:CFG") == ("readback", "DEV:RB")


def test_config_is_the_identity_without_setpoint_or_readback():
    assert _identity(None, None, "DEV:CFG") == ("config", "DEV:CFG")


def test_identity_requires_at_least_one_address():
    with pytest.raises(BadRequest):
        _identity(None, None, None)


async def test_create_many_rejects_readbacks_duplicated_within_the_import():
    rows = [NewPVElementDTO(readback_address="DEV:RB"), NewPVElementDTO(readback_address="DEV:RB")]
    with pytest.raises(BadRequest, match="readback"):
        await _service().create_many(rows)


async def test_create_many_rejects_config_addresses_duplicated_within_the_import():
    rows = [NewPVElementDTO(config_address="DEV:CFG"), NewPVElementDTO(config_address="DEV:CFG")]
    with pytest.raises(BadRequest, match="config"):
        await _service().create_many(rows)
