"""Typed ``Annotated[..., Depends(...)]`` aliases for Hare's API layer."""

from typing import Annotated

from fastapi import Depends

from canopy.hare import gateway
from canopy.hare.services.broker import PVBroker


def get_pv_broker() -> PVBroker:
    return gateway.get_broker()


PVBrokerDep = Annotated[PVBroker, Depends(get_pv_broker)]
