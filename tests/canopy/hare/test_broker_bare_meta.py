"""PVBroker: a describe that comes back bare counts as missing metadata.

A connector that cannot describe a channel in time returns a `PVMeta` naming
only the PV. The broker caches and sends it, but treats it like a failed
describe: the retry deadline is set, a later subscribe after the backoff
describes again, and so does the PV's next connect edge.
"""

import pytest

from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, ValueKind
from tests.canopy.hare.conftest import FakeWebSocket, settle
from canopy.hare.services.broker import PVBroker


def _bare_describes(connector, *, bare: int) -> list[str]:
    """Make the first ``bare`` describes of ``connector`` return bare metadata; record every call."""
    calls: list[str] = []

    async def describe(pv: str) -> PVMeta:
        calls.append(pv)
        if len(calls) <= bare:
            return PVMeta(pv=pv)
        return PVMeta(pv=pv, value_kind=ValueKind.SCALAR, units="mm", precision=3)

    connector.describe = describe
    return calls


def _metas(ws: FakeWebSocket, pv: str) -> list[dict]:
    return [m["meta"][pv] for m in ws.sent if m["type"] == "initial" and pv in m["meta"]]


async def test_bare_metadata_is_sent_but_sets_the_retry_deadline(fake_connector):
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    describes = _bare_describes(fake_connector, bare=1)
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:BARE"])
    await settle(broker)

    assert describes == ["PV:BARE"]
    assert _metas(ws, "PV:BARE")[-1]["units"] is None
    assert broker._meta_is_missing("PV:BARE")
    assert "PV:BARE" in broker._seed_retry_after


@pytest.mark.parametrize(
    ("backoff_s", "expected_describes"),
    [pytest.param(3600, 1, id="within-backoff"), pytest.param(0, 2, id="after-backoff")],
)
async def test_bare_metadata_is_described_again_by_a_later_subscribe_after_backoff(
    fake_connector, backoff_s, expected_describes
):
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=backoff_s))
    describes = _bare_describes(fake_connector, bare=1)
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:BARE"])
    await settle(broker)

    late = FakeWebSocket()
    broker.add_client("c2", late)
    await broker.subscribe("c2", ["PV:BARE"])
    await settle(broker)

    assert describes == ["PV:BARE"] * expected_describes
    assert broker._meta_is_missing("PV:BARE") is (expected_describes == 1)
    if expected_describes == 2:
        assert _metas(late, "PV:BARE")[-1]["units"] == "mm"
        assert "PV:BARE" not in broker._seed_retry_after


async def test_a_reconnect_describes_a_pv_whose_metadata_is_bare(fake_connector):
    """The connect edge describes again whatever the deadline: the channel answers now."""
    broker = PVBroker(fake_connector, HareConfig(seed_retry_backoff_s=3600))
    describes = _bare_describes(fake_connector, bare=1)
    ws = FakeWebSocket()
    broker.add_client("c1", ws)
    await broker.subscribe("c1", ["PV:BARE"])
    await settle(broker)

    fake_connector.emit("PV:BARE", None, connected=False)
    await settle(broker)
    assert describes == ["PV:BARE"]
    fake_connector.emit("PV:BARE", 2.0)
    await settle(broker)

    assert describes == ["PV:BARE", "PV:BARE"]
    assert broker._meta["PV:BARE"].units == "mm"
    assert _metas(ws, "PV:BARE")[-1]["units"] == "mm"


async def test_full_metadata_is_not_described_again_on_reconnect(fake_connector):
    broker = PVBroker(fake_connector, HareConfig())
    describes = _bare_describes(fake_connector, bare=0)
    broker.add_client("c1", FakeWebSocket())
    await broker.subscribe("c1", ["PV:FULL"])
    await settle(broker)
    fake_connector.emit("PV:FULL", None, connected=False)
    await settle(broker)
    fake_connector.emit("PV:FULL", 2.0)
    await settle(broker)
    assert describes == ["PV:FULL"]
