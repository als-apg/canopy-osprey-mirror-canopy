"""Dropping a lost Channel Access channel so that its next use searches anew.

The connector reads aioca's channel table through ``epics_backend._ca_channels``.
These tests swap that table, and aioca's ``caget`` / ``caput`` / ``camonitor``, for
fakes that keep the same bookkeeping, so the drop policy is tested without a live
IOC. ``test_ca_reconnect_after_outage.py`` walks the same paths against a soft IOC.
"""

import asyncio
import logging

import pytest

pytest.importorskip("aioca")
pytest.importorskip("p4p")

from aioca import CANothing, _catools

from tests.conftest import wait_for
from canopy.epics.config import EpicsConfig
from canopy.epics.backends import epics_backend
from canopy.epics.backends.epics_backend import EpicsConnector

PV = "TEST:RECONNECT"


class FakeChannel:
    """Stand-in for an aioca ``Channel``: its connection state and how many open
    subscriptions it carries."""

    def __init__(self, *, connected: bool) -> None:
        self.is_connected = connected
        self.subscriptions = 0
        self.purged = False

    def connected(self) -> bool:
        return self.is_connected

    def count_subscriptions(self) -> int:
        return self.subscriptions

    def _purge(self) -> None:
        self.purged = True


class FakeSubscription:
    """Stand-in for an aioca ``Subscription``. Like aioca's, it is counted on its
    channel only while it is open."""

    OPENING = 0
    OPEN = 1
    CLOSED = 2

    def __init__(self, channel: FakeChannel, state: int) -> None:
        self.channel = channel
        self.state = state
        if state == self.OPEN:
            channel.subscriptions += 1

    def close(self) -> None:
        if self.state == self.OPEN:
            self.channel.subscriptions -= 1
        self.state = self.CLOSED


class NotOk:
    """A failed ``caget``/``caput`` result, as aioca returns it with ``throw=False``."""

    ok = False
    errorcode = "timeout"


class FakeCA:
    """aioca's channel table plus the calls that open channels in it. A name not
    in the table gets a new channel, connected or not as `connect_new` says."""

    def __init__(self) -> None:
        self.channels: dict[str, FakeChannel] = {}
        self.connect_new = True
        self.callbacks: list = []

    def channel(self, name: str) -> FakeChannel:
        if name not in self.channels:
            self.channels[name] = FakeChannel(connected=self.connect_new)
        return self.channels[name]

    async def caget(self, name, **_kwargs):
        return 1.5 if self.channel(name).connected() else NotOk()

    async def caput(self, name, _value, **_kwargs):
        return CANothing(name) if self.channel(name).connected() else NotOk()

    async def cainfo(self, name, **_kwargs):
        raise RuntimeError("no cainfo here")

    def camonitor(self, name, callback, **_kwargs):
        channel = self.channel(name)
        self.callbacks.append(callback)
        return FakeSubscription(channel, FakeSubscription.OPEN if channel.connected() else FakeSubscription.OPENING)


@pytest.fixture
def ca(monkeypatch) -> FakeCA:
    fake = FakeCA()
    monkeypatch.setattr(epics_backend, "_ca_channels", lambda: fake.channels)
    monkeypatch.setattr(epics_backend, "caget", fake.caget)
    monkeypatch.setattr(epics_backend, "caput", fake.caput)
    monkeypatch.setattr(epics_backend, "cainfo", fake.cainfo)
    monkeypatch.setattr(epics_backend, "camonitor", fake.camonitor)
    return fake


async def _connector(**kwargs) -> EpicsConnector:
    connector = EpicsConnector.from_config(EpicsConfig(backend="epics", **kwargs))
    await connector.start()
    return connector


async def _seen_then_lost(ca: FakeCA, connector: EpicsConnector) -> FakeChannel:
    """Read `PV` once while it is connected, then lose its server."""
    assert (await connector.get(PV)).connected
    channel = ca.channels[PV]
    channel.is_connected = False
    ca.connect_new = False
    return channel


# --- one-shot reads, writes and describes ------------------------------------ #


async def test_read_drops_a_lost_channel_seen_connected(ca):
    connector = await _connector(ca_timeout_s=0.0)
    lost = await _seen_then_lost(ca, connector)

    assert not (await connector.get(PV)).connected
    assert lost.purged
    assert ca.channels[PV] is not lost, "the read did not open a new channel"


async def test_write_drops_a_lost_channel_seen_connected(ca):
    connector = await _connector(ca_timeout_s=0.0)
    lost = await _seen_then_lost(ca, connector)

    with pytest.raises(epics_backend.PVWriteFailed):
        await connector.put(PV, 2.0)
    assert lost.purged


async def test_describe_drops_a_lost_channel_seen_connected(ca):
    connector = await _connector(ca_timeout_s=0.0)
    lost = await _seen_then_lost(ca, connector)

    await connector.describe(PV)
    assert lost.purged


async def test_a_single_good_read_is_enough_to_count_as_seen(ca):
    connector = await _connector(ca_timeout_s=0.0)
    assert (await connector.get(PV)).connected
    assert PV in connector._ca_seen


async def test_never_connected_channel_keeps_libcas_backoff(ca):
    ca.connect_new = False
    connector = await _connector(ca_timeout_s=0.0)

    for _ in range(3):
        assert not (await connector.get(PV)).connected
    assert not ca.channels[PV].purged


async def test_drop_waits_out_ca_timeout_since_the_last_one(ca):
    connector = await _connector(ca_timeout_s=60.0)
    # Seen connected on a second read, so the first open does not hold off the drop.
    await connector.get(PV)
    lost = await _seen_then_lost(ca, connector)

    await connector.get(PV)
    assert lost.purged
    reopened = ca.channels[PV]
    await connector.get(PV)
    assert not reopened.purged, "the channel opened a moment ago was dropped again"


async def test_a_foreign_open_subscription_keeps_the_channel(ca):
    connector = await _connector(ca_timeout_s=0.0)
    lost = await _seen_then_lost(ca, connector)
    lost.subscriptions = 1  # opened elsewhere in the process

    await connector.get(PV)
    assert not lost.purged


async def test_a_foreign_subscription_still_opening_is_not_seen(ca):
    # The documented limit: aioca counts a subscription on its channel only once it
    # has connected, so one still waiting for its first connection is dropped too.
    connector = await _connector(ca_timeout_s=0.0)
    lost = await _seen_then_lost(ca, connector)
    foreign = FakeSubscription(lost, FakeSubscription.OPENING)

    await connector.get(PV)
    assert lost.purged
    assert foreign.state == FakeSubscription.OPENING


async def test_zero_turns_the_reconnection_off(ca):
    connector = await _connector(ca_timeout_s=0.0, ca_reconnect_s=0)
    lost = await _seen_then_lost(ca, connector)

    await connector.get(PV)
    assert not lost.purged


async def test_a_failure_turns_the_reconnection_off_and_reads_go_on(ca, monkeypatch, caplog):
    def broken():
        raise AttributeError("aioca moved its channel cache")

    monkeypatch.setattr(epics_backend, "_ca_channels", broken)
    connector = await _connector(ca_timeout_s=0.0)

    with caplog.at_level(logging.ERROR, logger=epics_backend.__name__):
        first = await connector.get(PV)
        second = await connector.get(PV)
    assert first.connected and second.connected
    assert connector._ca_reconnect == 0
    assert len(caplog.records) == 1


# --- monitors ---------------------------------------------------------------- #


async def _monitor_seen(ca: FakeCA, connector: EpicsConnector, events: list):
    subscription = await connector.subscribe(PV, events.append)
    ca.callbacks[-1](1.5)
    assert await wait_for(lambda: events and events[-1].connected)
    return subscription


async def test_own_monitors_move_to_the_new_channel(ca):
    connector = await _connector(ca_timeout_s=0.0)
    events: list = []
    subscription = await _monitor_seen(ca, connector, events)
    old_sub = subscription.sub
    lost = ca.channels[PV]
    lost.is_connected = False

    await connector.get(PV)
    assert lost.purged
    assert old_sub.state == FakeSubscription.CLOSED
    assert subscription.sub is not old_sub and subscription.sub.channel is ca.channels[PV]
    await subscription.close()
    assert PV not in connector._ca_monitors


async def test_a_monitor_that_cannot_reopen_does_not_strand_the_others(ca, monkeypatch, caplog):
    connector = await _connector(ca_timeout_s=0.0)
    events: list = []
    subscriptions = [await _monitor_seen(ca, connector, events) for _ in range(2)]
    ca.channels[PV].is_connected = False
    reopen = ca.camonitor
    failures = []

    def camonitor_failing_once(name, callback, **kwargs):
        if not failures:
            failures.append(name)
            raise RuntimeError("camonitor refused")
        return reopen(name, callback, **kwargs)

    monkeypatch.setattr(epics_backend, "camonitor", camonitor_failing_once)
    with caplog.at_level(logging.ERROR, logger=epics_backend.__name__):
        await connector.get(PV)

    assert failures == [PV]
    reopened = [s for s in subscriptions if s.sub is not None]
    assert len(reopened) == 1, "the monitor after the failed one was not reopened"
    assert reopened[0].sub.channel is ca.channels[PV]
    assert len(caplog.records) == 1
    assert connector._ca_reconnect > 0, "one monitor failing to reopen turned the reconnection off"
    assert PV in connector._ca_watchers, "no retry was armed for the monitor that failed to reopen"
    for subscription in subscriptions:
        await subscription.close()
    await connector.close()


async def test_a_monitor_that_failed_to_reopen_is_retried_once_the_channel_connects(ca, monkeypatch):
    # The sibling monitor brings the new channel back, so no further drop happens:
    # the watch must still reopen the monitor that failed.
    connector = await _connector(ca_reconnect_s=0.05)
    events: list = []
    subscriptions = [await _monitor_seen(ca, connector, events) for _ in range(2)]
    ca.channels[PV].is_connected = False
    ca.connect_new = False
    reopen = ca.camonitor
    failures = []

    def camonitor_failing_once(name, callback, **kwargs):
        if not failures:
            failures.append(name)
            raise RuntimeError("camonitor refused")
        return reopen(name, callback, **kwargs)

    monkeypatch.setattr(epics_backend, "camonitor", camonitor_failing_once)
    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: failures)
    stranded = next(s for s in subscriptions if s.sub is None)

    ca.connect_new = True
    ca.channels[PV].is_connected = True
    assert await wait_for(lambda: stranded.sub is not None), "the monitor that failed to reopen stayed closed"
    assert all(s.sub.channel is ca.channels[PV] for s in subscriptions)
    assert await wait_for(lambda: PV not in connector._ca_watchers)
    for subscription in subscriptions:
        await subscription.close()
    await connector.close()


async def test_a_monitor_that_keeps_failing_to_reopen_is_logged_as_an_error_once(ca, monkeypatch, caplog):
    connector = await _connector(ca_reconnect_s=0.05)
    events: list = []
    subscription = await _monitor_seen(ca, connector, events)
    ca.channels[PV].is_connected = False
    ca.connect_new = False
    reopen = ca.camonitor
    failures = []
    refuse = [True]

    def camonitor_refusing(name, callback, **kwargs):
        if refuse[0]:
            failures.append(name)
            raise RuntimeError("camonitor refused")
        return reopen(name, callback, **kwargs)

    def errors():
        return [r for r in caplog.records if r.levelno >= logging.ERROR]

    monkeypatch.setattr(epics_backend, "camonitor", camonitor_refusing)
    with caplog.at_level(logging.DEBUG, logger=epics_backend.__name__):
        ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
        assert await wait_for(lambda: len(failures) >= 3)
        assert len(errors()) == 1, "a monitor that kept failing to reopen logged an error at every tick"
        assert errors()[0].exc_info is not None

        refuse[0] = False
        assert await wait_for(lambda: subscription.sub is not None)
        assert not subscription.failing

        # A reopen that succeeded resets the flag: the next failure is an error again.
        refuse[0] = True
        subscription.detach()
        connector._ca_watch(PV)
        assert await wait_for(lambda: len(errors()) == 2)
    await subscription.close()
    await connector.close()


async def test_lost_monitor_is_dropped_by_its_timer_until_it_reconnects(ca):
    connector = await _connector(ca_reconnect_s=0.05)
    events: list = []
    subscription = await _monitor_seen(ca, connector, events)
    lost = ca.channels[PV]
    lost.is_connected = False
    ca.connect_new = False

    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: lost.purged)
    assert len(ca.callbacks) >= 2, "the monitor was not reopened on a new channel"
    # The new channel is still lost, so the timer stays armed.
    assert await wait_for(lambda: PV in connector._ca_watchers)

    # The new channel connects: the next tick finds it connected and ends the watch.
    ca.connect_new = True
    ca.channels[PV].is_connected = True
    assert await wait_for(lambda: PV not in connector._ca_watchers)
    await subscription.close()
    await connector.close()


async def test_never_connected_monitor_is_not_watched(ca):
    ca.connect_new = False
    connector = await _connector(ca_reconnect_s=0.05)
    events: list = []
    subscription = await connector.subscribe(PV, events.append)

    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: events)
    assert PV not in connector._ca_watchers
    await subscription.close()


async def test_close_cancels_the_watch_timers(ca):
    connector = await _connector(ca_reconnect_s=60.0)
    events: list = []
    await _monitor_seen(ca, connector, events)
    ca.channels[PV].is_connected = False

    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: PV in connector._ca_watchers)
    timer = connector._ca_watchers[PV]
    await connector.close()
    assert timer.cancelled()
    assert not connector._ca_watchers

    # A monitor its caller left open reports another disconnect.
    events.clear()
    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: events)
    assert not connector._ca_watchers, "a closed connector armed a new timer"


async def test_a_failure_on_the_monitor_path_is_logged_once(ca, monkeypatch, caplog):
    connector = await _connector(ca_reconnect_s=0.05)
    names = [f"{PV}:{i}" for i in range(3)]
    events: list = []
    subscriptions = []
    for name in names:
        subscriptions.append(await connector.subscribe(name, events.append))
        ca.callbacks[-1](1.5)
    assert await wait_for(lambda: len(events) == len(names))
    for name, callback in zip(names, ca.callbacks):
        ca.channels[name].is_connected = False
        callback(CANothing(name, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: set(connector._ca_watchers) == set(names))

    def broken():
        raise AttributeError("aioca moved its channel cache")

    monkeypatch.setattr(epics_backend, "_ca_channels", broken)
    with caplog.at_level(logging.ERROR, logger=epics_backend.__name__):
        assert await wait_for(lambda: not connector._ca_watchers)
        await asyncio.sleep(0.1)
    assert connector._ca_reconnect == 0
    assert len(caplog.records) == 1
    for subscription in subscriptions:
        await subscription.close()
    await connector.close()


async def test_zero_arms_no_watch_timer(ca):
    connector = await _connector(ca_reconnect_s=0)
    events: list = []
    await _monitor_seen(ca, connector, events)
    ca.channels[PV].is_connected = False

    ca.callbacks[-1](CANothing(PV, _catools.cadef.ECA_DISCONN))
    assert await wait_for(lambda: not events[-1].connected)
    assert not connector._ca_watchers


# --- aioca's private layout -------------------------------------------------- #


async def test_channel_table_is_aiocas_private_cache(monkeypatch):
    # The reconnection turns itself off when this layout changes; this test is what
    # makes an aioca release that changes it fail CI instead.
    loop = asyncio.get_running_loop()
    cache = _catools.ChannelCache(loop)
    monkeypatch.setitem(_catools._Context._channel_caches, loop, cache)
    assert epics_backend._ca_channels() is cache._ChannelCache__channels


async def test_channel_table_is_none_before_aioca_opens_a_channel(monkeypatch):
    monkeypatch.delitem(_catools._Context._channel_caches, asyncio.get_running_loop(), raising=False)
    assert epics_backend._ca_channels() is None


def test_aioca_channel_and_subscription_keep_the_members_the_drop_uses():
    for member in ("connected", "count_subscriptions", "_purge"):
        assert callable(getattr(_catools.Channel, member, None)), f"aioca Channel has no {member}"
    assert _catools.Subscription.OPEN != _catools.Subscription.OPENING
    assert callable(getattr(_catools.Subscription, "close", None))
