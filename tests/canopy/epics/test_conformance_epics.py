"""The EPICS backend (Channel Access and PV Access) against the conformance suite.

The suite needs a control system to talk to, and this host may already serve one
on the default EPICS ports. So the whole run happens in a helper process whose
environment confines both protocols to loopback on ports chosen here: that
process starts a caproto soft IOC (Channel Access) as its own child and a p4p
server (PV Access) in-process, runs every case against a real `EpicsConnector`,
and writes the outcomes to a file. Each test below then reports one case.

The helper is this file, run as a script with ``--helper``.
"""

import os
import sys
import json
import socket
import asyncio
import subprocess
from pathlib import Path

import pytest

from canopy.contracts.pv import PVWriteValue
from canopy.epics.config import EpicsConfig
from canopy.epics.gateway import build_connector
from canopy.epics.testing import conformance
from canopy.epics.pv_protocol import parse_pv_name

# Ports this host's own control systems listen on; a run must never use them.
_FORBIDDEN_CA_PORTS = {5064, 5065, 10090}

_PORT_ENV = "CANOPY_CONFORMANCE_CA_PORT"
_PREFIX_ENV = "CANOPY_CONFORMANCE_PREFIX"

_HELPER_TIMEOUT_S = 240
_IOC_START_TIMEOUT_S = 30.0

_NAME_PREFIX = {"bare": "", "ca": "ca://", "pva": "pva://"}

_IOC_SOURCE = """
import sys
from caproto import ChannelType
from caproto.server import PVGroup, pvproperty, run


class ConformanceIOC(PVGroup):
    scalar = pvproperty(value=0.0, name="SCALAR")
    enum = pvproperty(value="A", name="ENUM", enum_strings=["A", "B", "C", "D"], dtype=ChannelType.ENUM)
    waveform = pvproperty(value=[0.0, 0.0, 0.0], name="WAVEFORM")


run(ConformanceIOC(prefix=sys.argv[1]).pvdb, interfaces=["127.0.0.1"])
"""


# --------------------------------------------------------------------------- #
# Helper process: isolated environment, soft IOC, PVA server, the suite.
# --------------------------------------------------------------------------- #


class EpicsDriver:
    """Serves the suite's PVs: bare and ``ca://`` names from a caproto soft IOC
    in a child process, ``pva://`` names from an in-process p4p server."""

    def __init__(self, prefix: str, ca_port: int) -> None:
        self._prefix = prefix
        self._ca_port = ca_port
        self._ioc: subprocess.Popen | None = None
        self._pva_server = None
        self._pva_pvs: dict = {}

    # --- names ---

    def pv(self, kind: str, form: str) -> str:
        protocol = "PVA" if form == "pva" else "CA"
        if kind == conformance.NEVER:
            return f"{_NAME_PREFIX[form]}{self._prefix}ABSENT:{form.upper()}"
        return f"{_NAME_PREFIX[form]}{self._prefix}{protocol}:{kind.upper()}"

    # --- lifecycle ---

    def _assert_isolated(self) -> None:
        """Refuse to touch Channel Access unless it is confined to the test port."""
        assert os.environ.get("EPICS_CA_SERVER_PORT") == str(self._ca_port), "CA server port is not the test's port"
        assert self._ca_port not in _FORBIDDEN_CA_PORTS, f"CA port {self._ca_port} belongs to a real control system"
        assert os.environ.get("EPICS_CA_AUTO_ADDR_LIST") == "NO", "CA address list is not confined"
        assert os.environ.get("EPICS_CA_ADDR_LIST") == "127.0.0.1", "CA address list is not loopback"

    async def start(self) -> None:
        from p4p.nt import NTEnum, NTScalar
        from p4p.server import Server
        from p4p.server.thread import SharedPV

        self._assert_isolated()
        if not self._pva_pvs:
            base = f"{self._prefix}PVA:"
            self._pva_pvs = {
                f"{base}SCALAR": SharedPV(nt=NTScalar("d"), initial=0.0),
                f"{base}ENUM": SharedPV(nt=NTEnum(), initial={"index": 0, "choices": ["A", "B", "C", "D"]}),
                f"{base}WAVEFORM": SharedPV(nt=NTScalar("ad"), initial=[0.0, 0.0, 0.0]),
            }
        self._pva_server = Server(providers=[self._pva_pvs])
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

    # --- the conformance driver ---

    async def set_value(self, pv: str, value: PVWriteValue) -> None:
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            self._pva_pvs[stripped].post(value)
            return
        from caproto.sync.client import write

        self._assert_isolated()
        await asyncio.to_thread(write, stripped, value, notify=True, timeout=5.0, repeater=False)

    async def drop(self) -> None:
        await self.stop()

    async def restore(self) -> None:
        await self.start()


def _make_connector():
    return build_connector(EpicsConfig(backend="epics"))


async def _run_helper() -> dict:
    # A Channel Access client starts a detached `caRepeater` when nothing holds
    # the repeater port, and that process outlives the run. Holding the port here
    # makes the client take a repeater as present; nothing in the suite needs one.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as repeater_port:
        repeater_port.bind(("", int(os.environ["EPICS_CA_REPEATER_PORT"])))
        driver = EpicsDriver(os.environ[_PREFIX_ENV], int(os.environ[_PORT_ENV]))
        await driver.start()
        try:
            return await conformance.run_all(_make_connector, driver)
        finally:
            await driver.stop()


def _helper_main(result_path: str) -> None:
    results = asyncio.run(_run_helper())
    tmp = Path(result_path + ".tmp")
    tmp.write_text(json.dumps(results))
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


def _isolated_env() -> dict[str, str]:
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
            _PREFIX_ENV: f"CANOPY:CONFORMANCE:{os.getpid()}:",
        }
    )
    return env


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory) -> dict:
    """Run the whole suite once in the isolated helper process."""
    result_path = tmp_path_factory.mktemp("conformance-epics") / "outcomes.json"
    proc = subprocess.run(
        [sys.executable, __file__, "--helper", str(result_path)],
        env=_isolated_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_HELPER_TIMEOUT_S,
    )
    assert proc.returncode == 0 and result_path.exists(), (
        f"the conformance helper exited {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    )
    return json.loads(result_path.read_text())


@pytest.mark.parametrize("case_id", conformance.case_ids())
def test_conformance(case_id, outcomes):
    result = outcomes[case_id]
    if result["outcome"] == "skipped":
        pytest.skip(result["detail"])
    if result["outcome"] != "passed":
        pytest.fail(result["detail"], pytrace=False)


if __name__ == "__main__":
    assert sys.argv[1] == "--helper", "this file is a pytest module; --helper is for its own use"
    _helper_main(sys.argv[2])
