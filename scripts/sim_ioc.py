"""caproto soft IOC for verifying Hare's EPICS connector backend.

Hosts a few CA PVs under the ``TEST:`` prefix and updates twice a second so camonitor has
something to stream.

Run (loopback only)::

    EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
        uv run --with caproto python scripts/sim_ioc.py
"""

import math
import time

from caproto.server import PVGroup, run, pvproperty


class CanopyTestIOC(PVGroup):
    counter = pvproperty(value=0, name="COUNTER", doc="Increments every 0.5s")
    sine = pvproperty(value=0.0, name="SINE", doc="Sine wave, updates every 0.5s")
    const = pvproperty(value=42.0, name="CONST", doc="Constant value")

    @counter.scan(period=0.5)
    async def counter(self, instance, async_lib):
        await instance.write((instance.value or 0) + 1)

    @sine.scan(period=0.5)
    async def sine(self, instance, async_lib):
        await instance.write(round(math.sin(time.time()), 4))


if __name__ == "__main__":
    ioc = CanopyTestIOC(prefix="TEST:")
    run(ioc.pvdb)
