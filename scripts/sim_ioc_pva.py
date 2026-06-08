"""p4p PVA soft IOC for verifying Hare's PVAccess connector backend.

Hosts a few PVA PVs under the ``TEST:`` prefix and updates twice a second so
pvmonitor has something to stream.

Run::

    uv run --with p4p python scripts/sim_ioc_pva.py
"""

import math
import time
import threading

from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV


def make_pvs():
    counter = SharedPV(nt=NTScalar("i"), initial=0)
    sine = SharedPV(nt=NTScalar("d"), initial=0.0)
    const = SharedPV(nt=NTScalar("d"), initial=42.0)

    @counter.put
    def on_counter_put(pv, op):
        pv.post(op.value())
        op.done()

    @sine.put
    def on_sine_put(pv, op):
        pv.post(op.value())
        op.done()

    @const.put
    def on_const_put(pv, op):
        pv.post(op.value())
        op.done()

    return {
        "TEST:COUNTER": counter,
        "TEST:SINE": sine,
        "TEST:CONST": const,
    }


def updater(pvs, stop_event: threading.Event):
    count = 0
    while not stop_event.is_set():
        count += 1
        pvs["TEST:COUNTER"].post(count)
        pvs["TEST:SINE"].post(round(math.sin(time.time()), 4))
        stop_event.wait(0.5)


if __name__ == "__main__":
    pvs = make_pvs()
    stop = threading.Event()

    update_thread = threading.Thread(target=updater, args=(pvs, stop), daemon=True)
    update_thread.start()

    print("PVA IOC running. PVs: TEST:COUNTER, TEST:SINE, TEST:CONST")
    print("Press Ctrl+C to stop.")

    with Server(providers=[{"TEST:COUNTER": pvs["TEST:COUNTER"],
                            "TEST:SINE": pvs["TEST:SINE"],
                            "TEST:CONST": pvs["TEST:CONST"]}]):
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            update_thread.join()
            print("\nStopped.")
