"""Through canopy_mcp's Hare forward: a PV unreachable at subscribe time later connects with units.

The real Hare WebSocket endpoint runs under uvicorn in a background thread with a
``SlowConnector`` whose dead name comes alive mid-test; canopy_mcp relays the
browser-side socket to it. The client first sees a disconnected ``initial`` with
empty meta, then -- after the IOC "comes up" -- an ``initial`` whose meta carries
the units (b6:IA-1, b6:M-1). canopy_mcp adds no describe of its own: the forward
is read-only (405 for the describe REST path).
"""

from __future__ import annotations

import time
import asyncio
import threading
from pathlib import Path
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy.hare.api.v1 import ws as ws_module
from canopy.core.security import ws_require_read_access
from canopy.hare.dependencies import get_pv_broker
from canopy.hare.services.broker import PVBroker
from tests.canopy.hare.test_broker_seeding import DEAD_DELAY_S, SlowConnector, _config


class HareServer:
    """A started PVBroker behind the real ``/hare/v1/ws/pvs`` endpoint, on its own loop and port."""

    def __init__(self) -> None:
        self.connector = SlowConnector(dead={"DEAD:1"})
        self.loop: asyncio.AbstractEventLoop | None = None
        self.broker: PVBroker | None = None
        app = FastAPI()
        app.include_router(ws_module.router, prefix="/hare/v1")

        @app.on_event("startup")
        async def _start() -> None:
            self.loop = asyncio.get_running_loop()
            self.broker = PVBroker(self.connector, _config())
            await self.broker.start()

        @app.on_event("shutdown")
        async def _stop() -> None:
            assert self.broker is not None
            await self.broker.stop()

        app.dependency_overrides[get_pv_broker] = lambda: self.broker
        app.dependency_overrides[ws_require_read_access] = lambda: None
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("hare server did not start")
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def bring_up(self, pv: str, value: float) -> None:
        """Make ``pv`` reachable and deliver one connected monitor event, on the server's loop."""
        assert self.loop is not None

        def _go() -> None:
            self.connector.dead.discard(pv)
            self.connector.emit(pv, value, ts=50)

        self.loop.call_soon_threadsafe(_go)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def hare() -> Iterator[HareServer]:
    server = HareServer()
    yield server
    server.stop()


@pytest.fixture
def client(tmp_path: Path, hare: HareServer) -> Iterator[TestClient]:
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    settings = Settings(base_url=f"http://127.0.0.1:{hare.port}", api_key="k", drafts_dir=drafts)
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


def _receive_until(ws, predicate, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError("expected frame not received in time")


def test_late_connect_initial_carries_units_through_the_forward(client: TestClient, hare: HareServer) -> None:
    with client.websocket_connect("/hare/v1/ws/pvs") as ws:
        ws.send_json({"type": "subscribe", "pvNames": ["DEAD:1"]})
        dead = _receive_until(ws, lambda f: f["type"] == "initial" and "DEAD:1" in f["data"], DEAD_DELAY_S * 4)
        assert dead["data"]["DEAD:1"]["connected"] is False
        assert dead["meta"]["DEAD:1"]["units"] is None

        hare.bring_up("DEAD:1", 7.0)
        alive = _receive_until(
            ws,
            lambda f: f["type"] == "initial" and f.get("meta", {}).get("DEAD:1", {}).get("units") == "mm",
            DEAD_DELAY_S * 4,
        )
        assert alive["data"]["DEAD:1"]["value"] == 7.0
        assert alive["data"]["DEAD:1"]["connected"] is True


def test_describe_rest_path_stays_405_through_the_forward(client: TestClient) -> None:
    assert client.get("/hare/v1/pvs/DEAD:1/describe").status_code == 405
