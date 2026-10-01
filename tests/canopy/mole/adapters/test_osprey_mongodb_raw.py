"""OSPREY MongoDB archiver-recorder adapter: raw reads (FR8).

Pins ``fetch_window`` and ``fetch_last_before`` against a real throwaway
MongoDB: the Mole contract invariants, the ``max_raw`` cap (400, never a
truncated answer), the exact-nanosecond post-filter around the
millisecond-floored/ceiled Mongo bounds, value mapping, the last-before
lookback, and a document written exactly the way OSPREY's
``ArchiveWriter.write_sample`` writes it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from canopy.contracts.sample import AlarmSeverity, Sample
from canopy.core.errors import BadRequest
from canopy.mole.adapters.base import PVNotFoundInArchiver
from canopy.mole.adapters.osprey_mongodb import OspreyMongoRecorderAdapter
from canopy.mole.config import ArchiverInstanceConfig
from tests.canopy.mole.adapters import _mongo_seed as seed
from tests.canopy.mole.adapters.contract import (
    assert_fetch_last_before,
    assert_fetch_window_invariants,
)
from tests.canopy.mole.adapters.conftest import PASSWORD_ENV

pytestmark = pytest.mark.mongo

COLLECTION = "pv_history"
PV_A = "BTS:DIAG:BPM:01:POSITION:X"
PV_B = "BTS:DIAG:BPM:02:POSITION:X"
PV_S = "BTS:DIAG:STATE"

#: A fixed whole-second epoch (2026-09-30T12:00:00Z) so expectations are exact.
BASE_S = 1_790_769_600
BASE_NS = BASE_S * 1_000_000_000
NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000


def _cfg(url: str, **extras) -> ArchiverInstanceConfig:
    return ArchiverInstanceConfig(
        type="osprey_mongodb_recorder",
        url=url,
        api_key_env=PASSWORD_ENV,
        timeout_s=10.0,
        extras={"collection": COLLECTION, **extras},
    )


@pytest.fixture
async def make_adapter():
    """Build adapters and close every one of them at teardown."""
    made: list[OspreyMongoRecorderAdapter] = []

    def make(cfg: ArchiverInstanceConfig, name: str = "osprey") -> OspreyMongoRecorderAdapter:
        adapter = OspreyMongoRecorderAdapter.from_config(name, cfg)
        assert adapter.config_error is None, adapter.config_error
        made.append(adapter)
        return adapter

    yield make
    for adapter in made:
        await adapter.close()


@pytest.fixture
def coll(mongo_client, mongo_db_name):
    collection = mongo_client[mongo_db_name][COLLECTION]
    seed.prepare_collection(collection)
    return collection


def _seed_seconds(coll, count: int, *, pv: str = PV_A, start_s: int = BASE_S) -> None:
    """One recorder sample per second for ``count`` seconds, value = index."""
    seed.seed_samples(coll, ((seed.utc_slot(start_s + i), {pv: float(i)}) for i in range(count)))


def _at_ms(ms_after_base: int) -> datetime:
    return datetime.fromtimestamp(BASE_S, tz=UTC) + timedelta(milliseconds=ms_after_base)


# --------------------------------------------------------------------------- #
# fetch_window
# --------------------------------------------------------------------------- #


async def test_fetch_window_meets_the_contract_invariants(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 10)
    seed.write_manifest(coll)
    adapter = make_adapter(_cfg(mongo_url))

    await assert_fetch_window_invariants(adapter, PV_A, BASE_NS, BASE_NS + 9 * NS_PER_S)

    samples = await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 9 * NS_PER_S)
    assert [s.ts_ns for s in samples] == [BASE_NS + i * NS_PER_S for i in range(10)]
    assert [s.value for s in samples] == [float(i) for i in range(10)]
    assert all(s.severity is AlarmSeverity.NO_ALARM for s in samples)
    assert all(s.source == "archive" for s in samples)


async def test_fetch_window_is_inclusive_and_bounded(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 10)
    adapter = make_adapter(_cfg(mongo_url))

    samples = await adapter.fetch_window(PV_A, BASE_NS + 2 * NS_PER_S, BASE_NS + 5 * NS_PER_S)
    assert [s.value for s in samples] == [2.0, 3.0, 4.0, 5.0]


async def test_fetch_window_post_filters_to_the_exact_nanosecond(mongo_url, coll, make_adapter):
    """Mongo bounds are floored/ceiled to the ms; the answer is exact to the ns."""
    seed.write_sample(coll, _at_ms(0), {PV_A: 1.0})
    seed.write_sample(coll, _at_ms(5), {PV_A: 2.0})
    seed.write_sample(coll, _at_ms(10), {PV_A: 3.0})
    adapter = make_adapter(_cfg(mongo_url))

    # t0 one ns after the first sample, t1 one ns before the last: only the middle.
    samples = await adapter.fetch_window(PV_A, BASE_NS + 1, BASE_NS + 10 * NS_PER_MS - 1)
    assert [(s.ts_ns, s.value) for s in samples] == [(BASE_NS + 5 * NS_PER_MS, 2.0)]

    # A sub-ms window around a stored ms that does not contain it: nothing.
    t = BASE_NS + 5 * NS_PER_MS
    assert await adapter.fetch_window(PV_A, t + 1, t + 999) == []
    assert await adapter.fetch_window(PV_A, t - 999, t - 1) == []
    # A zero-width window exactly on it: that one sample.
    assert [s.value for s in await adapter.fetch_window(PV_A, t, t)] == [2.0]


async def test_fetch_window_skips_documents_without_the_pv(mongo_url, coll, make_adapter):
    seed.write_sample(coll, seed.utc_slot(BASE_S), {PV_A: 1.0, PV_B: 10.0})
    seed.write_sample(coll, seed.utc_slot(BASE_S + 1), {PV_B: 11.0})
    seed.write_sample(coll, seed.utc_slot(BASE_S + 2), {PV_A: 3.0, PV_B: 12.0})
    adapter = make_adapter(_cfg(mongo_url))

    samples = await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 2 * NS_PER_S)
    assert [(s.ts_ns, s.value) for s in samples] == [
        (BASE_NS, 1.0),
        (BASE_NS + 2 * NS_PER_S, 3.0),
    ]


async def test_fetch_window_maps_bool_and_int_to_float_and_keeps_strings(mongo_url, coll, make_adapter):
    coll.insert_many(
        [
            {"date": seed.utc_slot(BASE_S), PV_A: True, PV_S: "OPEN"},
            {"date": seed.utc_slot(BASE_S + 1), PV_A: 7, PV_S: "CLOSED"},
            {"date": seed.utc_slot(BASE_S + 2), PV_A: 2.5, PV_S: "OPEN"},
            {"date": seed.utc_slot(BASE_S + 3), PV_A: False, PV_S: "MOVING"},
        ]
    )
    adapter = make_adapter(_cfg(mongo_url))

    numeric = await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 3 * NS_PER_S)
    assert [s.value for s in numeric] == [1.0, 7.0, 2.5, 0.0]
    assert all(type(s.value) is float for s in numeric)

    strings = await adapter.fetch_window(PV_S, BASE_NS, BASE_NS + 3 * NS_PER_S)
    assert [s.value for s in strings] == ["OPEN", "CLOSED", "OPEN", "MOVING"]


async def test_fetch_window_at_the_cap_answers(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 5)
    adapter = make_adapter(_cfg(mongo_url, max_raw=5))

    samples = await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 10 * NS_PER_S)
    assert len(samples) == 5


async def test_fetch_window_over_the_cap_is_a_400_not_a_truncation(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 6)
    adapter = make_adapter(_cfg(mongo_url, max_raw=5))

    with pytest.raises(BadRequest) as info:
        await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 10 * NS_PER_S)
    assert info.value.status_code == 400
    assert "max_raw" in info.value.detail and "5" in info.value.detail

    # A narrower window under the cap still answers.
    samples = await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 4 * NS_PER_S)
    assert len(samples) == 5


async def test_fetch_window_cap_comes_from_extras_as_a_string(mongo_url, coll, make_adapter):
    """The Mole env delivers extras as strings; ``from_config`` coerces them."""
    _seed_seconds(coll, 4)
    adapter = make_adapter(_cfg(mongo_url, max_raw="3"))

    with pytest.raises(BadRequest):
        await adapter.fetch_window(PV_A, BASE_NS, BASE_NS + 10 * NS_PER_S)


async def test_fetch_window_unknown_pv_is_not_found(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 3)
    adapter = make_adapter(_cfg(mongo_url))

    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window(PV_B, BASE_NS, BASE_NS + 3 * NS_PER_S)


async def test_fetch_window_empty_and_inverted_ranges(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 3)
    adapter = make_adapter(_cfg(mongo_url))

    assert await adapter.fetch_window(PV_A, BASE_NS + 100 * NS_PER_S, BASE_NS + 200 * NS_PER_S) == []
    assert await adapter.fetch_window(PV_A, BASE_NS + 2 * NS_PER_S, BASE_NS) == []


# --------------------------------------------------------------------------- #
# fetch_last_before
# --------------------------------------------------------------------------- #


async def test_fetch_last_before_meets_the_contract(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 5)
    adapter = make_adapter(_cfg(mongo_url))

    await assert_fetch_last_before(adapter, PV_A, BASE_NS + 3 * NS_PER_S + 500)
    prior = await adapter.fetch_last_before(PV_A, BASE_NS + 3 * NS_PER_S + 500)
    assert isinstance(prior, Sample)
    assert (prior.ts_ns, prior.value) == (BASE_NS + 3 * NS_PER_S, 3.0)
    assert prior.source == "archive"
    assert prior.severity is AlarmSeverity.NO_ALARM


async def test_fetch_last_before_includes_an_exact_hit(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 5)
    adapter = make_adapter(_cfg(mongo_url))

    prior = await adapter.fetch_last_before(PV_A, BASE_NS + 2 * NS_PER_S)
    assert prior is not None
    assert (prior.ts_ns, prior.value) == (BASE_NS + 2 * NS_PER_S, 2.0)


async def test_fetch_last_before_one_ns_before_a_stored_ms(mongo_url, coll, make_adapter):
    """``$lte`` is floored to the ms, so a stored ms one ns later is never returned."""
    seed.write_sample(coll, _at_ms(0), {PV_A: 1.0})
    seed.write_sample(coll, _at_ms(7), {PV_A: 2.0})
    adapter = make_adapter(_cfg(mongo_url))

    prior = await adapter.fetch_last_before(PV_A, BASE_NS + 7 * NS_PER_MS - 1)
    assert prior is not None
    assert (prior.ts_ns, prior.value) == (BASE_NS, 1.0)


async def test_fetch_last_before_within_the_lookback(mongo_url, coll, make_adapter):
    seed.write_sample(coll, seed.utc_slot(BASE_S), {PV_A: 4.0})
    adapter = make_adapter(_cfg(mongo_url, last_before_lookback_s=60))

    prior = await adapter.fetch_last_before(PV_A, BASE_NS + 59 * NS_PER_S)
    assert prior is not None and prior.value == 4.0
    # Exactly at the lookback edge still counts.
    edge = await adapter.fetch_last_before(PV_A, BASE_NS + 60 * NS_PER_S)
    assert edge is not None and edge.ts_ns == BASE_NS


async def test_fetch_last_before_outside_the_lookback_is_none(mongo_url, coll, make_adapter):
    seed.write_sample(coll, seed.utc_slot(BASE_S), {PV_A: 4.0})
    adapter = make_adapter(_cfg(mongo_url, last_before_lookback_s=60))

    assert await adapter.fetch_last_before(PV_A, BASE_NS + 61 * NS_PER_S) is None


async def test_fetch_last_before_nothing_earlier_is_none(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 3)
    adapter = make_adapter(_cfg(mongo_url))

    assert await adapter.fetch_last_before(PV_A, BASE_NS - 1) is None


async def test_fetch_last_before_skips_documents_without_the_pv(mongo_url, coll, make_adapter):
    seed.write_sample(coll, seed.utc_slot(BASE_S), {PV_A: 1.0, PV_B: 10.0})
    seed.write_sample(coll, seed.utc_slot(BASE_S + 1), {PV_B: 11.0})
    adapter = make_adapter(_cfg(mongo_url))

    prior = await adapter.fetch_last_before(PV_A, BASE_NS + 5 * NS_PER_S)
    assert prior is not None
    assert (prior.ts_ns, prior.value) == (BASE_NS, 1.0)


async def test_fetch_last_before_unknown_pv_is_not_found(mongo_url, coll, make_adapter):
    _seed_seconds(coll, 3)
    adapter = make_adapter(_cfg(mongo_url))

    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_last_before(PV_B, BASE_NS + NS_PER_S)


# --------------------------------------------------------------------------- #
# The recorder's own write shape
# --------------------------------------------------------------------------- #


async def test_reads_back_a_document_shaped_like_the_recorder_writes(mongo_url, coll, make_adapter):
    """One document written as ``ArchiveWriter.write_sample`` does: upsert on
    ``date``, ``$set`` of float readings, ``$setOnInsert`` of ``expireAt``."""
    ts = datetime.fromtimestamp(BASE_S, tz=UTC)
    coll.update_one(
        {"date": ts},
        {
            "$set": {PV_A: 0.125, PV_B: -3.5},
            "$setOnInsert": {"expireAt": seed.expiry_for(ts)},
        },
        upsert=True,
    )
    seed.write_manifest(coll)
    adapter = make_adapter(_cfg(mongo_url))

    window = await adapter.fetch_window(PV_A, BASE_NS - NS_PER_S, BASE_NS + NS_PER_S)
    assert window == [Sample(ts_ns=BASE_NS, value=0.125, severity=AlarmSeverity.NO_ALARM, source="archive")]
    prior = await adapter.fetch_last_before(PV_B, BASE_NS + NS_PER_S)
    assert prior == Sample(ts_ns=BASE_NS, value=-3.5, severity=AlarmSeverity.NO_ALARM, source="archive")
