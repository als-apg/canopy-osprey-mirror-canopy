"""Fox inputs in an open Vole outage fail at once, never with a stale or refilled value.

A PV whose Hare connection dropped keeps its last sample in the ring, and
``VoleDataSource.read_latest`` would refill a missing ring from the archive.
Either would show an operator a number that is no longer true. So the
``DataSource`` port answers ``in_open_outage``; the scalar and waveform
evaluators check it before reading and raise ``FormulaInputOutage`` -- a
``FormulaInputMissing`` (code ``formula_input_missing``, retryable) -- and a live
subscription forwards it without the usual input-wait silence.
"""

import asyncio

import pytest

from canopy.fox.ports import DataSource, NullDataSource
from canopy.core.clock import FakeClock
from canopy.fox.config import FoxConfig
from canopy.fox.errors import FormulaInputOutage, FormulaInputMissing, is_retryable
from canopy.core.errors import ErrorCode
from canopy.vole.config import VoleConfig
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.vole.buffer.store import PVStore
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.services.parser import Parser
from canopy.vole.services.notifier import ChangeNotifier
from canopy.fox.services.formula_service import FormulaService
from canopy.vole.services.buffer_service import BufferService
from canopy.fox.services.scalar_evaluator import evaluate_scalar_formula
from canopy.vole.services.eviction_service import EvictionService
from canopy.fox.services.waveform_evaluator import evaluate_waveform_formula
from canopy.vole.services.alignment_service import AlignmentService
from canopy.fox.services.subscription_manager import SubscriptionManager

NOW = 1_000_000_000_000


def _sample(value, ts_ns: int = 1000) -> Sample:
    return Sample(ts_ns=ts_ns, value=value, severity=AlarmSeverity.NO_ALARM, source="live")


def _scalar(expr: str = "{a} + {b}", **bindings: str) -> FormulaSpec:
    return FormulaSpec(kind="scalar", expression=expr, bindings=bindings or {"a": "PV:A", "b": "PV:B"})


def _waveform(**bindings: str) -> FormulaSpec:
    return FormulaSpec(kind="waveform", expression="{w} * 2", bindings=bindings or {"w": "PV:WF"})


class _RecordingSource(FakeDataSource):
    """FakeDataSource that records every ``read_latest`` name."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reads: list[str] = []

    async def read_latest(self, pv: str) -> Sample | None:
        self.reads.append(pv)
        return await super().read_latest(pv)


def _buffer(notifier: ChangeNotifier | None = None) -> BufferService:
    config = VoleConfig(max_points=100, global_bytes_max=1_000_000)
    store = PVStore(config)
    return BufferService(
        store, AlignmentService(), EvictionService(store, config), FakeClock(start_ns=NOW), config, notifier=notifier
    )


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.messages.append(msg)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _manager(data_source, notifier: ChangeNotifier, **kwargs) -> SubscriptionManager:
    async def subscribe_live(pvs: list[str]) -> None:
        return None

    return SubscriptionManager(
        formula_service=FormulaService(data_source, Parser(), FoxConfig()),
        on_change_fn=notifier.subscribe,
        subscribe_live_fn=subscribe_live,
        subscribe_latest_fn=subscribe_live,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The error and the port
# --------------------------------------------------------------------------- #


class TestFormulaInputOutage:
    def test_is_a_formula_input_missing_with_the_retryable_code(self):
        exc = FormulaInputOutage("PVs in outage: ['PV:A']")
        assert isinstance(exc, FormulaInputMissing)
        assert exc.error_code is ErrorCode.FORMULA_INPUT_MISSING
        assert exc.error_code.value == "formula_input_missing"
        assert exc.status_code == 422
        assert is_retryable(exc.error_code.value) is True


class TestPort:
    def test_null_data_source_has_no_open_outage(self):
        assert NullDataSource().in_open_outage("PV:A") is False

    def test_fake_data_source_has_no_open_outage_by_default(self):
        assert FakeDataSource().in_open_outage("PV:A") is False

    def test_every_data_source_satisfies_the_port(self):
        assert isinstance(NullDataSource(), DataSource)
        assert isinstance(FakeDataSource(), DataSource)
        assert isinstance(VoleDataSource(_buffer()), DataSource)

    def test_a_source_without_in_open_outage_no_longer_satisfies_the_port(self):
        class _Old:
            async def read_aligned(self, *a, **k): ...

            async def read_binned(self, *a, **k): ...

            async def read_latest(self, pv): ...

        assert not isinstance(_Old(), DataSource)


# --------------------------------------------------------------------------- #
# Evaluators
# --------------------------------------------------------------------------- #


class TestScalarEvaluator:
    async def test_input_in_outage_raises_at_once_without_reading_the_stale_sample(self):
        source = _RecordingSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:B"})
        service = FormulaService(source, Parser(), FoxConfig())
        with pytest.raises(FormulaInputOutage) as info:
            await service.evaluate(_scalar(), t0_ns=0, t1_ns=0)
        assert "PV:B" in info.value.detail
        assert "PV:B" not in source.reads

    async def test_every_outage_input_is_named(self):
        source = _RecordingSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:A", "PV:B"})
        with pytest.raises(FormulaInputOutage) as info:
            await FormulaService(source, Parser(), FoxConfig()).evaluate(_scalar(), t0_ns=0, t1_ns=0)
        assert "PV:A" in info.value.detail and "PV:B" in info.value.detail
        assert source.reads == []

    async def test_outage_wins_over_a_missing_input(self):
        source = _RecordingSource(latest={"PV:A": _sample(5.0)}, outages={"PV:A"})
        with pytest.raises(FormulaInputOutage):
            await FormulaService(source, Parser(), FoxConfig()).evaluate(_scalar(), t0_ns=0, t1_ns=0)

    async def test_missing_input_without_outage_stays_a_plain_formula_input_missing(self):
        source = _RecordingSource(latest={"PV:A": _sample(5.0)})
        with pytest.raises(FormulaInputMissing) as info:
            await FormulaService(source, Parser(), FoxConfig()).evaluate(_scalar(), t0_ns=0, t1_ns=0)
        assert not isinstance(info.value, FormulaInputOutage)

    async def test_no_outage_evaluates_normally(self):
        source = _RecordingSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)})
        result = await evaluate_scalar_formula(
            _scalar(), Parser().parse("{a} + {b}", {"a": "PV:A", "b": "PV:B"}), source, FoxConfig()
        )
        assert result.value == pytest.approx(8.0)


class TestWaveformEvaluator:
    async def test_input_in_outage_raises_at_once_without_reading_the_stale_sample(self):
        source = _RecordingSource(latest={"PV:WF": _sample(bytes([1, 2, 3]))}, outages={"PV:WF"})
        with pytest.raises(FormulaInputOutage) as info:
            await FormulaService(source, Parser(), FoxConfig()).evaluate(_waveform(), t0_ns=0, t1_ns=0)
        assert "PV:WF" in info.value.detail
        assert source.reads == []

    async def test_no_outage_evaluates_normally(self):
        source = _RecordingSource(latest={"PV:WF": _sample(bytes([1, 2, 3]))})
        result = await evaluate_waveform_formula(
            _waveform(), Parser().parse("{w} * 2", {"w": "PV:WF"}), source, FoxConfig()
        )
        assert result.values == [2.0, 4.0, 6.0]


# --------------------------------------------------------------------------- #
# VoleDataSource
# --------------------------------------------------------------------------- #


class TestVoleDataSource:
    async def test_open_outage_on_a_ring_with_a_stale_sample_is_reported(self):
        buffer = _buffer()
        await buffer.append("PV:A", _sample(5.0))
        source = VoleDataSource(buffer)
        assert source.in_open_outage("PV:A") is False

        buffer.open_outage("PV:A", NOW + 1)
        assert source.in_open_outage("PV:A") is True
        assert source.in_open_outage("ca://PV:A") is True  # canonical name, as Vole keys rings

        buffer.close_outage("PV:A", NOW + 2)
        assert source.in_open_outage("PV:A") is False

    async def test_outage_before_the_ring_exists_is_reported(self):
        buffer = _buffer()
        buffer.open_outage("PV:A", NOW)
        assert VoleDataSource(buffer).in_open_outage("PV:A") is True

    async def test_unknown_pv_has_no_open_outage(self):
        assert VoleDataSource(_buffer()).in_open_outage("PV:NOPE") is False

    async def test_scalar_never_uses_the_stale_ring_sample(self):
        buffer = _buffer()
        await buffer.append("PV:A", _sample(5.0))
        await buffer.append("PV:B", _sample(3.0))
        buffer.open_outage("PV:B", NOW + 1)
        service = FormulaService(VoleDataSource(buffer), Parser(), FoxConfig())
        with pytest.raises(FormulaInputOutage):
            await service.evaluate(_scalar(), t0_ns=0, t1_ns=0)

    async def test_outage_input_is_never_refilled_from_the_archive(self):
        fetched: list[str] = []
        subscribed: list[list[str]] = []

        async def fetch_window(pv, t0_ns, t1_ns, **kwargs):
            fetched.append(pv)
            return [_sample(99.0, ts_ns=t1_ns - 1)]

        async def subscribe_live(pvs, **kwargs):
            subscribed.append(list(pvs))

        buffer = _buffer()
        buffer.open_outage("PV:WF", NOW)  # dead before its first sample: no ring
        source = VoleDataSource(
            buffer, fetch_window=fetch_window, subscribe_live=subscribe_live, clock=FakeClock(start_ns=NOW)
        )
        with pytest.raises(FormulaInputOutage):
            await FormulaService(source, Parser(), FoxConfig()).evaluate(_waveform(), t0_ns=0, t1_ns=0)
        assert fetched == []
        assert subscribed == []


# --------------------------------------------------------------------------- #
# Live subscriptions
# --------------------------------------------------------------------------- #


class TestLiveSubscription:
    async def test_outage_error_is_sent_without_the_input_wait(self):
        notifier = ChangeNotifier()
        source = FakeDataSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:B"})
        mgr = _manager(source, notifier, input_wait_notify_s=60.0)
        client = _Collector()
        await mgr.add_subscription("c1", "s1", _scalar(), t0_ns=0, t1_ns=0, send_fn=client.send)
        try:
            await _wait_for(lambda: client.of_type("error"), timeout=1.0)
            error = client.of_type("error")[0]
            assert error["code"] == "formula_input_missing"
            assert error["retryable"] is True
            assert "PV:B" in error["message"]
            assert client.of_type("result") == []
        finally:
            await mgr.shutdown()

    async def test_repeated_outage_retries_send_one_frame(self):
        notifier = ChangeNotifier()
        source = FakeDataSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:B"})
        mgr = _manager(source, notifier, input_wait_notify_s=60.0)
        client = _Collector()
        await mgr.add_subscription("c1", "s1", _scalar(), t0_ns=0, t1_ns=0, send_fn=client.send)
        try:
            await asyncio.sleep(0.8)  # several 0.25 s retries
            assert len(client.of_type("error")) == 1
        finally:
            await mgr.shutdown()

    async def test_poll_mode_outage_error_is_sent_without_the_input_wait(self):
        notifier = ChangeNotifier()
        source = FakeDataSource(latest={"PV:A": _sample(5.0), "PV:B": _sample(3.0)}, outages={"PV:B"})
        mgr = _manager(source, notifier, input_wait_notify_s=60.0)
        client = _Collector()
        await mgr.add_subscription("c1", "s1", _scalar(), t0_ns=0, t1_ns=0, send_fn=client.send, interval_ms=50)
        try:
            await _wait_for(lambda: client.of_type("error"), timeout=1.0)
            await asyncio.sleep(0.3)
            assert len(client.of_type("error")) == 1
        finally:
            await mgr.shutdown()

    async def test_plain_missing_input_still_waits_silently(self):
        notifier = ChangeNotifier()
        source = FakeDataSource(latest={"PV:A": _sample(5.0)})
        mgr = _manager(source, notifier, input_wait_notify_s=60.0)
        client = _Collector()
        await mgr.add_subscription("c1", "s1", _scalar(), t0_ns=0, t1_ns=0, send_fn=client.send)
        try:
            await asyncio.sleep(0.4)
            assert client.messages == []
        finally:
            await mgr.shutdown()

    async def test_vole_outage_open_and_close_drive_the_subscription(self):
        """End to end over a real buffer: result -> immediate outage error -> result again."""
        notifier = ChangeNotifier()
        buffer = _buffer(notifier)
        await buffer.append("PV:A", _sample(5.0))
        await buffer.append("PV:B", _sample(3.0))
        mgr = _manager(VoleDataSource(buffer), notifier, input_wait_notify_s=60.0)
        client = _Collector()
        await mgr.add_subscription("c1", "s1", _scalar(), t0_ns=0, t1_ns=0, send_fn=client.send)
        try:
            await _wait_for(lambda: client.of_type("result"))
            assert client.messages[-1]["data"]["scalar"]["value"] == pytest.approx(8.0)

            buffer.open_outage("PV:B", NOW + 1)
            await _wait_for(lambda: client.messages[-1]["type"] == "error", timeout=1.0)
            assert client.messages[-1]["code"] == "formula_input_missing"
            assert client.messages[-1]["retryable"] is True

            buffer.close_outage("PV:B", NOW + 2)
            await buffer.append("PV:B", _sample(4.0, ts_ns=2000))
            await _wait_for(lambda: client.messages[-1]["type"] == "result", timeout=1.0)
            assert client.messages[-1]["data"]["scalar"]["value"] == pytest.approx(9.0)
        finally:
            await mgr.shutdown()
