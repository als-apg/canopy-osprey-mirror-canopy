"""A live formula subscribe never waits for the broker's seed of its inputs.

The WS read loop awaits ``add_subscription`` inline, so one client's subscribe
frames are served one after another. Were each subscribe to wait for its inputs'
seed reads, every unseeded dead name would hold the client's one socket for the
connector's read timeout, and N dead inputs would delay everything queued behind
them by N timeouts. Here the stack is real from the subscription manager down to
the broker (Vole gateway, ingest, buffer, ``VoleDataSource``); only the connector
is fake, with dead names whose reads answer only after ``DEAD_DELAY_S``.
"""

from __future__ import annotations

import time
import asyncio
from dataclasses import field, dataclass

import pytest

from canopy.vole import gateway as vole_gateway
from canopy.core.clock import SystemClock
from canopy.fox.config import FoxConfig
from canopy.hare.config import HareConfig
from canopy.vole.config import VoleConfig
from canopy.contracts.pv import PVMeta, PVValue, ValueKind
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.buffer.store import PVStore
from canopy.fox.services.parser import Parser
from tests.canopy.hare.conftest import FakeConnector
from canopy.hare.services.broker import PVBroker
from canopy.vole.services.notifier import ChangeNotifier
from canopy.vole.services.hare_ingest import HareIngestService
from canopy.fox.services.formula_service import FormulaService
from canopy.vole.services.buffer_service import BufferService
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService
from canopy.fox.services.subscription_manager import SubscriptionManager

S = 1_000_000_000
DEAD_DELAY_S = 4.0  # the connector's read timeout for a dead name
NOTIFY_S = 0.5  # input_wait_notify_s
BOUND_S = NOTIFY_S + 1.0
N_DEAD = 12


@dataclass
class DeadNamesConnector(FakeConnector):
    """Reads of names in ``dead`` answer disconnected after ``DEAD_DELAY_S``; the rest answer at once.

    The broker hands the connector the name as bound (``ca://DEAD:0``), so ``dead`` holds canonical names.
    """

    dead: set[str] = field(default_factory=set)

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        if strip_protocol_prefix(pv) in self.dead:
            await asyncio.sleep(DEAD_DELAY_S)
            return PVValue(pv=pv, value=None, severity=AlarmSeverity.INVALID, ts_ns=1, connected=False, error="timeout")
        return PVValue(pv=pv, value=3.0, severity=AlarmSeverity.NO_ALARM, ts_ns=time.time_ns(), connected=True)

    async def describe(self, pv: str) -> PVMeta:
        if strip_protocol_prefix(pv) in self.dead:
            await asyncio.sleep(DEAD_DELAY_S)
            return PVMeta(pv=pv)
        return PVMeta(pv=pv, value_kind=ValueKind.SCALAR)


class ArchiveEverything:
    """Mole stand-in: every name is archived (a dead name too), and every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_window(self, pv, t0_ns, t1_ns, *, max_points=None):
        self.calls.append(pv)
        return [Sample(ts_ns=t1_ns - 3600 * S, value=-99.0, severity=AlarmSeverity.NO_ALARM, source="archive")]


@pytest.fixture
async def manager():
    """The Fox subscription manager wired as ``fox_lifespan`` wires it, over a real Vole and broker."""
    dead = {f"DEAD:{i}" for i in range(2 * N_DEAD)}
    connector = DeadNamesConnector(dead=dead)
    broker = PVBroker(connector, HareConfig(ws_batch_interval_ms=10, heartbeat_interval_s=100.0))
    await broker.start()
    config = VoleConfig(max_points=1000, global_bytes_max=10_000_000)
    store = PVStore(config)
    notifier = ChangeNotifier()
    buffer = BufferService(
        store, AlignmentService(), EvictionService(store, config), SystemClock(), config, notifier=notifier
    )
    ingest = HareIngestService(buffer, broker)
    await ingest.start()
    vole_gateway.bind(buffer)
    vole_gateway.bind_notifier(notifier)
    vole_gateway.bind_ingest(ingest)

    mole = ArchiveEverything()
    source = VoleDataSource(
        buffer,
        fetch_window=mole.fetch_window,
        subscribe_live=vole_gateway.subscribe_live,
        clock=SystemClock(),
        is_live_fed=ingest.is_subscribed,
    )
    fox_config = FoxConfig(input_wait_notify_s=NOTIFY_S)
    mgr = SubscriptionManager(
        formula_service=FormulaService(source, Parser(), fox_config),
        on_change_fn=vole_gateway.on_change,
        subscribe_live_fn=vole_gateway.subscribe_live,
        subscribe_latest_fn=vole_gateway.subscribe_latest,
        input_wait_notify_s=NOTIFY_S,
        pin_fn=vole_gateway.pin,
        unpin_fn=vole_gateway.unpin,
    )
    try:
        yield mgr, mole
    finally:
        await mgr.shutdown()
        vole_gateway.bind_ingest(None)
        vole_gateway.bind_notifier(None)
        vole_gateway.bind(None)
        await ingest.stop()
        await broker.stop()


def _scalar(expression: str, bindings: dict[str, str]) -> FormulaSpec:
    return FormulaSpec(kind="scalar", expression=expression, bindings=bindings)


@pytest.mark.parametrize(("branch", "t0_ns", "offset"), [("latest", 0, 0), ("live", -60 * S, N_DEAD)])
async def test_dead_inputs_never_hold_one_clients_subscribes(manager, branch, t0_ns, offset):
    mgr, mole = manager
    first: dict[str, tuple[float, dict]] = {}

    def _send_for(sub_id: str):
        async def send(frame: dict) -> None:
            first.setdefault(sub_id, (time.monotonic(), frame))

        return send

    # One client's frames, served in order as the WS read loop serves them: every
    # dead input first, then a live PV and a fox:// channel (a screen formula) behind them.
    subs: list[tuple[str, FormulaSpec]] = [
        (f"dead-{i}", _scalar("{a}", {"a": f"ca://DEAD:{offset + i}"})) for i in range(N_DEAD)
    ]
    subs.append(("live", _scalar("{a}", {"a": "ca://LIVE:1"})))
    subs.append(("fox", _scalar("{a} * 2", {"a": "LIVE:2"})))

    t0 = time.monotonic()
    for sub_id, spec in subs:
        await mgr.add_subscription("c1", sub_id, spec, t0_ns, 0, _send_for(sub_id))
    queued_s = time.monotonic() - t0
    assert queued_s < 1.0, f"{branch}: {len(subs)} subscribes held the client for {queued_s:.2f} s"

    deadline = t0 + BOUND_S + 0.5
    while len(first) < len(subs) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)

    for i in range(N_DEAD):
        at, frame = first[f"dead-{i}"]
        assert frame["type"] == "error" and frame["retryable"] is True, (branch, frame)
        assert at - t0 <= BOUND_S, f"{branch}: dead-{i} heard after {at - t0:.2f} s"
    for sub_id, value in (("live", 3.0), ("fox", 6.0)):
        at, frame = first[sub_id]
        assert frame["type"] == "result", (branch, sub_id, frame)
        assert frame["data"]["scalar"]["value"] == value
        assert at - t0 <= BOUND_S, f"{branch}: {sub_id} first result after {at - t0:.2f} s"
    # Live-fed inputs read their rings as they are: no archive value is ever served as live.
    assert mole.calls == []
