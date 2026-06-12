"""Verify Hare's real EPICS backend end-to-end against the soft IOCs.

Drives the actual ``PVBroker`` (the same object the app uses) with the real
``epics`` connector: a one-shot read + ``describe`` for every value kind, plus a
live monitor subscription.

Start the IOCs first (separate shells)::

    EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
        uv run --with caproto python scripts/sim_ioc.py
    uv run --with p4p python scripts/sim_ioc_pva.py

then::

    EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
        uv run python scripts/verify_epics.py

The PVA section is optional: if that IOC is not running, those rows just show
``connected=False`` and the run still PASSes on the CA checks.
"""

import asyncio

from canopy.hare.config import HareConfig
from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector
from canopy.hare.services.broker import PVBroker


class CollectChannel:
    """Stand-in WebSocket that records the frames the broker sends."""

    def __init__(self) -> None:
        self.msgs: list[dict] = []

    async def send_json(self, data) -> None:
        self.msgs.append(data)

    async def close(self) -> None:
        pass


def _abbrev(value, limit: int = 6) -> str:
    if isinstance(value, list) and len(value) > limit:
        head = ", ".join(repr(v) for v in value[:limit])
        return f"[{head}, ... +{len(value) - limit}]"
    return repr(value)


async def _probe(broker: PVBroker, pv: str):
    value = await broker.get_value(pv)
    meta = await broker.get_describe(pv)
    if meta.enum_choices:
        extra = f" choices={meta.enum_choices}"
    elif meta.units:
        extra = f" units={meta.units!r}"
    else:
        extra = ""
    print(
        f"  {pv:<26} connected={str(value.connected):<5} "
        f"kind={value.value_kind.value:<11} value={_abbrev(value.value)}{extra}"
    )
    return value


async def main() -> int:
    connector = build_connector(EpicsConfig(backend="epics", ca_timeout_s=3.0, pva_timeout_s=3.0))
    broker = PVBroker(connector, HareConfig(ws_batch_interval_ms=100))
    await broker.start()
    channel = CollectChannel()
    broker.add_client("verify", channel)

    print("Channel Access (TEST: via sim_ioc.py):")
    const = await _probe(broker, "TEST:CONST")
    for pv in ("TEST:ENUM", "TEST:WAVEFORM", "TEST:STRING", "TEST:LONGSTRING"):
        await _probe(broker, pv)

    print("PV Access (pva://TEST: via sim_ioc_pva.py):")
    for pv in ("pva://TEST:CONST", "pva://TEST:ENUM", "pva://TEST:WAVEFORM"):
        await _probe(broker, pv)
    image = await _probe(broker, "pva://TEST:IMAGE")

    # Live monitor: the CA counter should advance over the window.
    await broker.subscribe("verify", ["TEST:COUNTER"])
    await asyncio.sleep(2.0)
    await broker.stop()

    counter_values = [m["data"]["TEST:COUNTER"]["value"] for m in channel.msgs if "TEST:COUNTER" in m.get("data", {})]
    print(f"\nlive TEST:COUNTER values: {counter_values}")

    ok = const.connected and const.value == 42.0 and len(counter_values) >= 2 and counter_values[-1] > counter_values[0]
    if image.connected:  # only assert the image path when the PVA IOC is up
        ok = ok and image.value_kind.value == "unsupported"
    print("RESULT:", "PASS — real CA read + live monitor + typed dispatch working" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
