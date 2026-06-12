"""p4p PVA soft IOC for verifying Hare's PVAccess connector backend.

Hosts PVA PVs of every value kind under the ``TEST:`` prefix and updates them so
pvmonitor has something to stream:

    TEST:COUNTER   NTScalar int (increments)     TEST:ENUM      NTEnum (cycles)
    TEST:SINE      NTScalar double (sine)        TEST:WAVEFORM  NTScalarArray double
    TEST:CONST     NTScalar double (constant)    TEST:IMAGE     NTNDArray (8x8 image)

Run::

    uv run --with p4p python scripts/sim_ioc_pva.py
"""

import math
import time
import threading

import numpy as np
from p4p.nt import NTEnum, NTScalar, NTNDArray
from p4p.server import Server
from p4p.server.thread import SharedPV

_STATES = ["OFF", "LOW", "HIGH", "FAULT"]
_WF_LEN = 32


def make_pvs():
    pvs = {
        "TEST:COUNTER": SharedPV(nt=NTScalar("i"), initial=0),
        "TEST:SINE": SharedPV(nt=NTScalar("d"), initial=0.0),
        "TEST:CONST": SharedPV(nt=NTScalar("d"), initial=42.0),
        "TEST:ENUM": SharedPV(nt=NTEnum(), initial={"index": 0, "choices": _STATES}),
        "TEST:WAVEFORM": SharedPV(nt=NTScalar("ad"), initial=[0.0] * _WF_LEN),
        "TEST:IMAGE": SharedPV(nt=NTNDArray(), initial=np.zeros((8, 8), dtype=np.uint8)),
    }

    # Accept writes (echo) so the PVs are not read-only.
    for pv in pvs.values():

        @pv.put
        def on_put(pv, op):
            pv.post(op.value())
            op.done()

    return pvs


def updater(pvs, stop_event: threading.Event):
    count = 0
    while not stop_event.is_set():
        count += 1
        t = time.time()
        pvs["TEST:COUNTER"].post(count)
        pvs["TEST:SINE"].post(round(math.sin(t), 4))
        pvs["TEST:ENUM"].post({"index": count % len(_STATES), "choices": _STATES})
        pvs["TEST:WAVEFORM"].post([round(math.sin(t + i * 2 * math.pi / _WF_LEN), 4) for i in range(_WF_LEN)])
        pvs["TEST:IMAGE"].post((np.random.default_rng(count).integers(0, 255, size=(8, 8))).astype(np.uint8))
        stop_event.wait(0.5)


if __name__ == "__main__":
    pvs = make_pvs()
    stop = threading.Event()

    update_thread = threading.Thread(target=updater, args=(pvs, stop), daemon=True)
    update_thread.start()

    print("PVA IOC running. PVs: " + ", ".join(pvs))
    print("Press Ctrl+C to stop.")

    with Server(providers=[pvs]):
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            update_thread.join()
            print("\nStopped.")
