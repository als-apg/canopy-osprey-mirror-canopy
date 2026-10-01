"""OSPREY MongoDB archiver-recorder adapter: search, metadata and the Mole contract.

Runs the reusable conformance suite (``contract.py``, the
``test_fake_adapter_contract.py`` pattern) against an adapter over a seeded
throwaway MongoDB, pins ``search`` (regex validation, full match, sorting,
limit) and ``metadata``, and drives both through the Mole HTTP router with the
adapter registered as the default archiver.
"""

from __future__ import annotations

import httpx
import pytest

from canopy.core.errors import BadRequest
from canopy.main import app as parent_app
from canopy.mole.adapters.base import PVNotFoundInArchiver
from canopy.mole.adapters.osprey_mongodb import OspreyMongoRecorderAdapter
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.app import app as mole_app
from canopy.mole.config import ArchiverInstanceConfig, MoleConfig
from canopy.mole.dependencies import get_archiver_service
from canopy.mole.schemas.pv import PVMetadata
from canopy.mole.services.archiver_service import ArchiverService
from tests.canopy.mole.adapters import _mongo_seed as seed
from tests.canopy.mole.adapters import contract
from tests.canopy.mole.adapters.conftest import PASSWORD_ENV
from tests.conftest import READ_HEADERS

pytestmark = pytest.mark.mongo

COLLECTION = "pv_history"
NAME = "osprey"
BPM_01 = "BTS:DIAG:BPM:01:POSITION:X"
BPM_02 = "BTS:DIAG:BPM:02:POSITION:X"
BPM_10 = "BTS:DIAG:BPM:10:POSITION:X"
BPM_01_Y = "BTS:DIAG:BPM:01:POSITION:Y"
STATE = "BTS:DIAG:STATE"
CHANNELS = [BPM_01, BPM_02, BPM_10, BPM_01_Y, STATE]

#: A fixed whole-second epoch (2026-09-30T12:00:00Z) so expectations are exact.
BASE_S = 1_790_769_600
NS_PER_S = 1_000_000_000
BASE_NS = BASE_S * NS_PER_S
SECONDS = 20

BAD_PATTERN = "pattern is a regular expression (use .* not *)"


def _cfg(url: str) -> ArchiverInstanceConfig:
    return ArchiverInstanceConfig(
        type="osprey_mongodb_recorder",
        url=url,
        api_key_env=PASSWORD_ENV,
        timeout_s=10.0,
        extras={"collection": COLLECTION},
    )


@pytest.fixture
def coll(mongo_client, mongo_db_name):
    collection = mongo_client[mongo_db_name][COLLECTION]
    seed.prepare_collection(collection)
    seed.seed_samples(
        collection,
        ((seed.utc_slot(BASE_S + i), {pv: float(i + k) for k, pv in enumerate(CHANNELS)}) for i in range(SECONDS)),
    )
    seed.write_manifest(collection)
    return collection


@pytest.fixture
async def adapter(mongo_url, coll):
    a = OspreyMongoRecorderAdapter.from_config(NAME, _cfg(mongo_url))
    assert a.config_error is None, a.config_error
    yield a
    await a.close()


# --------------------------------------------------------------------------- #
# Conformance suite
# --------------------------------------------------------------------------- #


async def test_health(adapter):
    await contract.assert_health(adapter)
    assert (await adapter.health()).healthy


async def test_close_idempotent(mongo_url, coll):
    fresh = OspreyMongoRecorderAdapter.from_config("fresh", _cfg(mongo_url))
    await fresh.channel_index()  # opens the client, so close has something to close
    await contract.assert_close_idempotent(fresh)


async def test_fetch_window_invariants(adapter):
    await contract.assert_fetch_window_invariants(
        adapter, pv=BPM_01, t0_ns=BASE_NS, t1_ns=BASE_NS + (SECONDS - 1) * NS_PER_S
    )


async def test_fetch_last_before_invariants(adapter):
    await contract.assert_fetch_last_before(adapter, pv=BPM_01, ts_ns=BASE_NS + 5 * NS_PER_S)
    prior = await adapter.fetch_last_before(BPM_01, BASE_NS + 5 * NS_PER_S)
    assert prior is not None and prior.ts_ns == BASE_NS + 5 * NS_PER_S


async def test_fetch_binned_auto_invariants(adapter):
    await contract.assert_fetch_binned_auto(
        adapter, pv=BPM_01, t0_ns=BASE_NS, t1_ns=BASE_NS + (SECONDS - 1) * NS_PER_S, target_buckets=5
    )


async def test_unknown_pv_raises(adapter):
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window("PV:UNKNOWN", t0_ns=BASE_NS, t1_ns=BASE_NS + NS_PER_S)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


async def test_search_full_matches_and_sorts(adapter):
    assert await adapter.search("BTS:DIAG:BPM:.*:POSITION:X", limit=100) == [BPM_01, BPM_02, BPM_10]
    assert await adapter.search(".*", limit=100) == sorted(CHANNELS)


async def test_search_is_a_full_match_not_a_substring_search(adapter):
    assert await adapter.search("BPM", limit=100) == []
    assert await adapter.search("BTS:DIAG:BPM:01", limit=100) == []
    assert await adapter.search(STATE, limit=100) == [STATE]


async def test_search_supports_anchors_and_classes(adapter):
    assert await adapter.search("^BTS:DIAG:BPM:0[12]:POSITION:X$", limit=100) == [BPM_01, BPM_02]


async def test_search_applies_limit_after_sorting(adapter):
    assert await adapter.search(".*BPM.*", limit=2) == [BPM_01, BPM_01_Y]
    assert await adapter.search(".*", limit=1) == [sorted(CHANNELS)[0]]


async def test_search_excludes_bookkeeping_fields(adapter):
    found = await adapter.search(".*", limit=1000)
    assert not {"_id", "date", "expireAt"} & set(found)


@pytest.mark.parametrize(
    "pattern",
    [
        "*BPM*",  # a glob, not a regex: fails re.compile
        "BPM[",  # unterminated class
        "BTS:DIAG:BPM(01)",  # parentheses are outside the alphabet
        "BTS DIAG",  # space is outside the alphabet
        "a|b",  # alternation is outside the alphabet
        ".*a.*b.*c.*d",  # four stars
        "A" * 129,  # too long
    ],
)
async def test_search_rejects_bad_patterns(adapter, pattern):
    with pytest.raises(BadRequest) as exc:
        await adapter.search(pattern, limit=100)
    assert exc.value.detail == BAD_PATTERN


async def test_search_accepts_the_edges_of_the_limits(adapter):
    assert await adapter.search(".*a.*b.*c", limit=100) == []
    assert await adapter.search("A" * 128, limit=100) == []


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #


async def test_metadata_names_the_pv_and_archiver(adapter):
    meta = await adapter.metadata(BPM_01)
    assert meta == PVMetadata(pv=BPM_01, archiver=NAME)
    assert meta.units is None and meta.precision is None


async def test_metadata_of_an_unknown_pv_raises(adapter):
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.metadata("PV:UNKNOWN")


# --------------------------------------------------------------------------- #
# HTTP through the Mole router
# --------------------------------------------------------------------------- #


async def test_search_and_metadata_over_http(adapter):
    registry = AdapterRegistry({NAME: adapter}, default=NAME)
    service = ArchiverService(registry, MoleConfig(default_archiver=NAME))
    mole_app.dependency_overrides[get_archiver_service] = lambda: service
    try:
        transport = httpx.ASGITransport(app=parent_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/mole/v1/search",
                params={"pattern": "BTS:DIAG:BPM:.*:POSITION:X", "limit": 2},
                headers=READ_HEADERS,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["archiver"] == NAME
            assert body["pvs"] == [BPM_01, BPM_02]

            r = await client.get("/mole/v1/search", params={"pattern": "*BPM*"}, headers=READ_HEADERS)
            assert r.status_code == 400, r.text
            assert r.json()["code"] == "bad_request"
            assert r.json()["detail"] == BAD_PATTERN

            r = await client.get(f"/mole/v1/metadata/{BPM_01}", headers=READ_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["pv"] == BPM_01
            assert r.json()["archiver"] == NAME

            r = await client.get("/mole/v1/metadata/PV:UNKNOWN", headers=READ_HEADERS)
            assert r.status_code == 404, r.text
    finally:
        mole_app.dependency_overrides.pop(get_archiver_service, None)
