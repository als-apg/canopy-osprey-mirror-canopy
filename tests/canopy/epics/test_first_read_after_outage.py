"""One long-lived EPICS connector across a control-system outage.

A service builds its connector once and keeps it for the life of the process.
When the control system goes away for longer than a read's timeout and comes
back, the next read on that same connector has to answer the live value: nobody
restarts the service. Each scenario here walks that sequence against a real
`EpicsConnector`: start the soft IOC, read, stop it, wait past the read timeout,
start it again, read again on the same connector.

This host may already serve a control system on the default EPICS ports, and a
Channel Access client keeps its channels for the life of its process. So every
scenario runs in its own helper process, whose environment confines both
protocols to loopback on ports chosen here; the test reports what it measured.
The scenarios run side by side, since each spends most of its time waiting.

The helper is this file, run as a script with ``--helper``.
"""

import os
import sys
import json
import socket
import asyncio
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector

# Ports this host's own control systems listen on; a run must never use them.
_FORBIDDEN_CA_PORTS = {5064, 5065, 10090}

_PORT_ENV = "CANOPY_FIRST_READ_CA_PORT"
_PREFIX_ENV = "CANOPY_FIRST_READ_PREFIX"

_IOC_START_TIMEOUT_S = 30.0

# How long the control system stays away. The connector's read timeout is 5 s,
# and the outage has to outlast it by enough that the Channel Access library has
# slowed its own search for the lost channel. Measured here, libca searches 10.0,
# 10.05, 10.2, 10.6, 11.3, 12.3, 14.3, 20.5, 32.7, 49.1 and 98.3 s after it lost
# the server. An outage of 22 s ends just after one search, with the next one
# more than ten seconds away: a connector that only waits for libca answers
# nothing in the recovery window below.
_OUTAGE_S = 22.0
_READ_TIMEOUT_S = 5.0
# How soon after the control system is back a read must answer the live value:
# one read timeout.
_RECOVERY_S = 5.0
_POLL_S = 0.25

_FIRST = 1.5
_SECOND = 2.5

_IOC_SOURCE = """
import sys
from caproto.server import PVGroup, pvproperty, run


class OutageIOC(PVGroup):
    scalar = pvproperty(value=0.0, name="SCALAR")


run(OutageIOC(prefix=sys.argv[1]).pvdb, interfaces=["127.0.0.1"])
"""

# What each scenario does while the control system is away, whether a monitor
# is open on the PV the whole time (as it is for a PV a screen shows), and which
# of the two is watched first once the control system is back. A monitored PV
# that nobody reads has only the connector's own timer to bring it back, so that
# scenario shortens the timer's period to fit the recovery window.
SCENARIOS = {
    "ca-idle": {"form": "ca", "read_during": False, "monitor": False},
    "ca-read-during-outage": {"form": "ca", "read_during": True, "monitor": False},
    "ca-monitored-and-read": {"form": "ca", "read_during": True, "monitor": True},
    "ca-monitored-not-read": {"form": "ca", "read_during": False, "monitor": True, "ca_reconnect_s": 2.0},
    "pva-read-during-outage": {"form": "pva", "read_during": True, "monitor": False},
}


# --------------------------------------------------------------------------- #
# Helper process: isolated environment, soft IOC, PVA server, one scenario.
# --------------------------------------------------------------------------- #


class OutageDriver:
    """Serves one scalar PV per protocol and can take both away: Channel Access
    from a caproto soft IOC in a child process, PV Access from an in-process p4p
    server."""

    def __init__(self, prefix: str, ca_port: int) -> None:
        self._prefix = prefix
        self._ca_port = ca_port
        self._ioc: subprocess.Popen | None = None
        self._pva_server = None
        self._pva_pv = None

    def pv(self, form: str) -> str:
        if form == "pva":
            return f"pva://{self._prefix}PVA:SCALAR"
        return f"{self._prefix}CA:SCALAR"

    def _assert_isolated(self) -> None:
        """Refuse to touch Channel Access unless it is confined to the test port."""
        assert os.environ.get("EPICS_CA_SERVER_PORT") == str(self._ca_port), "CA server port is not the test's port"
        assert self._ca_port not in _FORBIDDEN_CA_PORTS, f"CA port {self._ca_port} belongs to a real control system"
        assert os.environ.get("EPICS_CA_AUTO_ADDR_LIST") == "NO", "CA address list is not confined"
        assert os.environ.get("EPICS_CA_ADDR_LIST") == "127.0.0.1", "CA address list is not loopback"

    async def start(self) -> None:
        from p4p.nt import NTScalar
        from p4p.server import Server
        from p4p.server.thread import SharedPV

        self._assert_isolated()
        if self._pva_pv is None:
            self._pva_pv = SharedPV(nt=NTScalar("d"), initial=0.0)
        self._pva_server = Server(providers=[{f"{self._prefix}PVA:SCALAR": self._pva_pv}])
        self._ioc = subprocess.Popen(
            [sys.executable, "-c", _IOC_SOURCE, f"{self._prefix}CA:"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_for_ioc()

    async def _wait_for_ioc(self) -> None:
        from caproto.sync.client import read

        name = f"{self._prefix}CA:SCALAR"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _IOC_START_TIMEOUT_S
        while True:
            assert self._ioc is not None and self._ioc.poll() is None, "the soft IOC exited"
            try:
                await asyncio.to_thread(read, name, timeout=0.5, repeater=False)
                return
            except Exception:
                assert loop.time() < deadline, f"the soft IOC did not serve {name} in {_IOC_START_TIMEOUT_S} s"

    async def stop(self) -> None:
        if self._pva_server is not None:
            self._pva_server.stop()
            self._pva_server = None
        if self._ioc is not None:
            self._ioc.kill()
            await asyncio.to_thread(self._ioc.wait)
            self._ioc = None

    async def set_value(self, form: str, value: float) -> None:
        """Set the PV from the control-system side, not through the connector."""
        if form == "pva":
            self._pva_pv.post(value)
            return
        from caproto.sync.client import write

        self._assert_isolated()
        name = f"{self._prefix}CA:SCALAR"
        await asyncio.to_thread(write, name, value, notify=True, timeout=5.0, repeater=False)


def _brief(value) -> dict:
    return {"connected": value.connected, "value": value.value, "error": value.error}


async def _read_until(connector, pv: str, expected: float, within_s: float) -> dict:
    """Read `pv` until it answers `expected` connected, or `within_s` has passed.

    Answers how long that took and what the last read said."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    reads = 0
    while True:
        value = await connector.get(pv)
        reads += 1
        elapsed = loop.time() - started
        ok = value.connected and value.value == expected
        if ok or elapsed >= within_s:
            return {"ok": ok, "seconds": round(elapsed, 3), "reads": reads, "last": _brief(value)}
        await asyncio.sleep(_POLL_S)


async def _monitor_until(events: list, expected: float, within_s: float) -> dict:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        ok = any(e.connected and e.value == expected for e in events)
        elapsed = loop.time() - started
        if ok or elapsed >= within_s:
            last = _brief(events[-1]) if events else None
            return {"ok": ok, "seconds": round(elapsed, 3), "events": len(events), "last": last}
        await asyncio.sleep(_POLL_S)


async def _run_scenario(spec: dict) -> dict:
    form = spec["form"]
    outage_s = spec.get("outage_s", _OUTAGE_S)
    observe_s = spec.get("observe_s", _RECOVERY_S)
    loop = asyncio.get_running_loop()
    record: dict = {"spec": spec}
    # A Channel Access client starts a detached `caRepeater` when nothing holds
    # the repeater port, and that process outlives the run. Holding the port here
    # makes the client take a repeater as present.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as repeater_port:
        repeater_port.bind(("", int(os.environ["EPICS_CA_REPEATER_PORT"])))
        driver = OutageDriver(os.environ[_PREFIX_ENV], int(os.environ[_PORT_ENV]))
        pv = driver.pv(form)
        await driver.start()
        # The one connector of the whole sequence, as in a running service.
        knobs = {k: spec[k] for k in ("ca_reconnect_s",) if k in spec}
        config = EpicsConfig(backend="epics", ca_timeout_s=_READ_TIMEOUT_S, pva_timeout_s=_READ_TIMEOUT_S, **knobs)
        connector = build_connector(config)
        await connector.start()
        subscription = None
        try:
            events: list = []
            if spec["monitor"]:
                subscription = await connector.subscribe(pv, events.append)
            await driver.set_value(form, _FIRST)
            record["before"] = await _read_until(connector, pv, _FIRST, 10.0)
            if spec["monitor"]:
                record["monitor_before"] = await _monitor_until(events, _FIRST, 10.0)

            await driver.stop()
            stopped = loop.time()
            during = []
            while (remaining := outage_s - (loop.time() - stopped)) > 0:
                # A read during the outage runs into its timeout; make one only
                # while it still ends before the outage does.
                if spec["read_during"] and remaining > _READ_TIMEOUT_S + 2 * _POLL_S:
                    during.append(_brief(await connector.get(pv)))
                await asyncio.sleep(min(_POLL_S, remaining))
            record["during"] = during
            record["outage_s"] = round(loop.time() - stopped, 3)

            await driver.start()
            # Written from the control-system side: it is serving again from here.
            await driver.set_value(form, _SECOND)
            if spec["monitor"] and not spec["read_during"]:
                # Nobody reads this PV: the monitor has to come back by itself.
                record["monitor_after"] = await _monitor_until(events, _SECOND, observe_s)
                record["after"] = await _read_until(connector, pv, _SECOND, observe_s)
            else:
                record["after"] = await _read_until(connector, pv, _SECOND, observe_s)
                if spec["monitor"]:
                    record["monitor_after"] = await _monitor_until(events, _SECOND, observe_s)
        finally:
            if subscription is not None:
                await subscription.close()
            await connector.close()
            await driver.stop()
    return record


def _helper_main(result_path: str, spec_json: str) -> None:
    record = asyncio.run(_run_scenario(json.loads(spec_json)))
    tmp = Path(result_path + ".tmp")
    tmp.write_text(json.dumps(record))
    tmp.rename(result_path)


# --------------------------------------------------------------------------- #
# pytest side
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    """A loopback port free for both TCP and UDP, outside the real CA ports."""
    for _ in range(200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            if port in _FORBIDDEN_CA_PORTS:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
    raise RuntimeError("no free loopback port found")


def _isolated_env(tag: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPICS_")}
    ports: list[int] = []
    while len(ports) < 4:
        port = _free_port()
        if port not in ports:
            ports.append(port)
    ca_port, repeater_port, pva_port, pva_broadcast_port = ports
    env.update(
        {
            "EPICS_CA_AUTO_ADDR_LIST": "NO",
            "EPICS_CA_ADDR_LIST": "127.0.0.1",
            "EPICS_CA_SERVER_PORT": str(ca_port),
            "EPICS_CA_REPEATER_PORT": str(repeater_port),
            "EPICS_CAS_INTF_ADDR_LIST": "127.0.0.1",
            "EPICS_CAS_AUTO_BEACON_ADDR_LIST": "NO",
            "EPICS_CAS_BEACON_ADDR_LIST": "127.0.0.1",
            "EPICS_PVA_AUTO_ADDR_LIST": "NO",
            "EPICS_PVA_ADDR_LIST": "127.0.0.1",
            "EPICS_PVA_SERVER_PORT": str(pva_port),
            "EPICS_PVA_BROADCAST_PORT": str(pva_broadcast_port),
            "EPICS_PVAS_INTF_ADDR_LIST": "127.0.0.1",
            _PORT_ENV: str(ca_port),
            _PREFIX_ENV: f"CANOPY:OUTAGE:{os.getpid()}:{tag}:",
        }
    )
    return env


def run_scenario(spec: dict, result_path: Path, tag: str) -> dict:
    """Walk one outage sequence in an isolated helper process; return its record."""
    outage_s = spec.get("outage_s", _OUTAGE_S)
    observe_s = spec.get("observe_s", _RECOVERY_S)
    # The outage, the two observation windows, the IOC starts and the reads
    # that time out during the outage, with room to spare.
    timeout_s = outage_s + 2 * observe_s + 2 * _IOC_START_TIMEOUT_S + 60
    proc = subprocess.run(
        [sys.executable, __file__, "--helper", str(result_path), json.dumps(spec)],
        env=_isolated_env(tag),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    assert proc.returncode == 0 and result_path.exists(), (
        f"the outage helper exited {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    )
    return json.loads(result_path.read_text())


@pytest.fixture(scope="module")
def records(tmp_path_factory) -> dict:
    """Walk every scenario once, each in its own helper process, side by side."""
    out = tmp_path_factory.mktemp("first-read-after-outage")

    def walk(scenario: str):
        try:
            return run_scenario(SCENARIOS[scenario], out / f"{scenario}.json", scenario)
        except BaseException as exc:  # reported by the scenario's own test
            return exc

    with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as pool:
        return dict(zip(SCENARIOS, pool.map(walk, SCENARIOS)))


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_read_after_outage_answers_the_live_value(scenario, records):
    spec = SCENARIOS[scenario]
    record = records[scenario]
    if isinstance(record, BaseException):
        raise record

    before = record["before"]
    assert before["ok"], f"the read before the outage did not answer {_FIRST}: {before}"

    assert record["outage_s"] >= _OUTAGE_S, f"the outage lasted only {record['outage_s']} s"
    if spec["read_during"]:
        during = record["during"]
        assert during, "no read was made during the outage"
        stale = [r for r in during if r["connected"] or r["value"] is not None]
        assert not stale, f"a read during the outage answered a value: {stale}"

    after = record["after"]
    assert after["ok"], (
        f"{after['seconds']} s after the control system was back, the same connector still did not "
        f"answer {_SECOND} ({after['reads']} reads; the last said {after['last']})"
    )

    if spec["monitor"]:
        assert record["monitor_before"]["ok"], f"the monitor never delivered {_FIRST}: {record['monitor_before']}"
        monitor_after = record["monitor_after"]
        assert monitor_after["ok"], (
            f"{monitor_after['seconds']} s into its recovery window, the monitor opened before the outage "
            f"still had not delivered {_SECOND} (the last event said {monitor_after['last']})"
        )


if __name__ == "__main__":
    assert sys.argv[1] == "--helper", "this file is a pytest module; --helper is for its own use"
    _helper_main(sys.argv[2], sys.argv[3])
