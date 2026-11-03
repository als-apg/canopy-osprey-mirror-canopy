"""Hare-scoped pytest fixtures.

`FakeConnector` is a deterministic connector: a monitor delivers one value on
subscribe, as a real monitor does on connect, unless the PV is in `dead`, and
after that only when the test calls `emit()` — used to drive the broker without
timing or external infra. `FakeWebSocket` captures the JSON the broker sends.
`settle()` ingests queued monitor updates and waits for the seeding they start.
Endpoint tests instead use the real simulation backend via the app lifespan.
"""

from dataclasses import field, dataclass

import pytest

from canopy.epics.base import Subscription, EventCallback
from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, PVValue, ValueKind
from canopy.contracts.sample import AlarmSeverity
from canopy.hare.services.broker import PVBroker


@dataclass
class _FakeSub:
    pv: str
    callbacks: dict

    async def close(self) -> None:
        self.callbacks.pop(self.pv, None)


@dataclass
class FakeConnector:
    backend_name: str = "fake"
    callbacks: dict[str, EventCallback] = field(default_factory=dict)
    started: bool = False
    closed: bool = False
    # Recorded `put` calls as (pv, value, wait, timeout); set `put_error` to make
    # the next put raise (drives the broker/endpoint write-failure paths).
    puts: list = field(default_factory=list)
    put_error: Exception | None = None
    # PVs whose monitor never delivers a value: channels that never connect.
    dead: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg) -> "FakeConnector":
        return cls()

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        self.callbacks.clear()

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        return PVValue(pv=pv, value=0.0, severity=AlarmSeverity.NO_ALARM, ts_ns=1, connected=True)

    async def put(self, pv: str, value, *, timeout: float | None = None, wait: bool = True) -> None:
        self.puts.append((pv, value, wait, timeout))
        if self.put_error is not None:
            raise self.put_error

    async def describe(self, pv: str) -> PVMeta:
        return PVMeta(pv=pv, value_kind=ValueKind.SCALAR, units="mm", precision=3)

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        self.callbacks[pv] = on_event
        if pv not in self.dead:
            on_event(PVValue(pv=pv, value=0.0, severity=AlarmSeverity.NO_ALARM, ts_ns=1, connected=True))
        return _FakeSub(pv, self.callbacks)

    def emit(
        self,
        pv: str,
        value,
        *,
        severity: AlarmSeverity = AlarmSeverity.NO_ALARM,
        ts: int = 2,
        connected: bool = True,
    ) -> None:
        """Synchronously deliver one update (as the connector callback would)."""
        callback = self.callbacks.get(pv)
        if callback is not None:
            callback(PVValue(pv=pv, value=value, severity=severity, ts_ns=ts, connected=connected))


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


async def settle(broker: PVBroker) -> None:
    """Ingest queued monitor updates, wait for the seeding they start and flush the diffs, as the running broker would."""
    await broker._drain_nowait()
    await broker.drain_seeds()
    await broker._drain_nowait()
    await broker._flush()


@pytest.fixture
def fake_connector() -> FakeConnector:
    return FakeConnector()


@pytest.fixture
def broker(fake_connector: FakeConnector) -> PVBroker:
    """Unstarted broker (no background loops); tests drive consume/flush directly."""
    return PVBroker(fake_connector, HareConfig(ws_batch_interval_ms=10, heartbeat_interval_s=100.0))
