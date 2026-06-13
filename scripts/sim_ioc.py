"""caproto soft IOC for verifying Hare's EPICS connector backend (Channel Access).

Hosts CA PVs of every value kind under the ``TEST:`` prefix and updates them so
camonitor has something to stream:

    TEST:COUNTER     scalar int (increments)        TEST:ENUM       enum (cycles)
    TEST:SINE        scalar double (sine)           TEST:WAVEFORM   double waveform
    TEST:CONST       scalar double (constant)       TEST:STRING     string
                                                     TEST:LONGSTRING DBR_CHAR string

Run (loopback only)::

    EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO \
        uv run --with caproto python scripts/sim_ioc.py
"""

import math
import time

from caproto import ChannelType
from caproto.server import PVGroup, run, pvproperty

_STATES = ["OFF", "LOW", "HIGH", "FAULT"]
_WF_LEN = 32


class CanopyTestIOC(PVGroup):
    counter = pvproperty(value=0, name="COUNTER", doc="Increments every 0.5s")
    sine = pvproperty(
        value=0.0,
        name="SINE",
        units="V",
        precision=3,
        lower_warning_limit=-7.0,
        upper_warning_limit=7.0,
        lower_alarm_limit=-9.0,
        upper_alarm_limit=9.0,
        lower_disp_limit=-10.0,
        upper_disp_limit=10.0,
        doc="Sine wave with warning/alarm limits",
    )
    const = pvproperty(value=42.0, name="CONST", units="mm", precision=2, doc="Constant value")
    state = pvproperty(
        value="OFF",
        name="ENUM",
        enum_strings=_STATES,
        dtype=ChannelType.ENUM,
        doc="Enum cycling once a second",
    )
    waveform = pvproperty(
        value=[0.0] * _WF_LEN,
        name="WAVEFORM",
        units="counts",
        precision=2,
        doc="Animated sine waveform",
    )
    message = pvproperty(value="idle", name="STRING", dtype=ChannelType.STRING, doc="A string PV")
    longstring = pvproperty(
        value="canopy-long-string",
        name="LONGSTRING",
        dtype=ChannelType.CHAR,
        max_length=64,
        doc="DBR_CHAR long-string idiom",
    )

    @counter.scan(period=0.5)
    async def counter(self, instance, async_lib):
        await instance.write((instance.value or 0) + 1)

    @sine.scan(period=0.5)
    async def sine(self, instance, async_lib):
        await instance.write(round(math.sin(time.time()), 4))

    @state.scan(period=1.0)
    async def state(self, instance, async_lib):
        await instance.write(_STATES[int(time.time()) % len(_STATES)])

    @waveform.scan(period=0.5)
    async def waveform(self, instance, async_lib):
        t = time.time()
        await instance.write([round(math.sin(t + i * 2 * math.pi / _WF_LEN), 4) for i in range(_WF_LEN)])


if __name__ == "__main__":
    ioc = CanopyTestIOC(prefix="TEST:")
    run(ioc.pvdb)
