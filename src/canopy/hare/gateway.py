"""Public surface of the Hare module for in-process callers.

Other Canopy modules import only from this file. Exposes live-PV reads; the
broker singleton is bound at app startup. (Live subscription streaming is a
WebSocket concern, not an in-process gateway call. Modules that need a value
read it here, clients stream it over the socket.)
"""

from canopy.contracts.pv import PVValue
from canopy.hare.services.broker import PVBroker

_broker: PVBroker | None = None


def bind(broker: PVBroker | None) -> None:
    """Wire (or unwire) the PVBroker singleton."""
    global _broker
    _broker = broker


def get_broker() -> PVBroker:
    if _broker is None:
        raise RuntimeError("Hare gateway accessed before app startup bound a broker")
    return _broker


async def get_value(pv: str) -> PVValue:
    """Current value of a PV (cached if monitored, else a one-shot read)."""
    return await get_broker().get_value(pv)
