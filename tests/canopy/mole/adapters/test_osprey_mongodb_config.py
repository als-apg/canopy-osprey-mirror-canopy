"""OSPREY MongoDB archiver-recorder adapter: config, channel index, health, errors.

The reads themselves (``fetch_window`` and friends) are covered by their own
module; this one pins what the adapter does before it ever reads a sample:
how it is configured from the Mole env, when it refuses to connect, which
fields count as channels, and how pymongo failures map onto Mole's 503/504/502.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import errors as mongo_errors

from canopy.mole.config import MoleConfig, ArchiverInstanceConfig, get_mole_config
from canopy.mole.adapters import osprey_mongodb
from canopy.mole.adapters.base import (
    AdapterHealth,
    ArchiverTimeout,
    ArchiverBadResponse,
    ArchiverUnavailable,
    PVNotFoundInArchiver,
    get_registered_type,
)
from tests.canopy.mole.adapters import _mongo_seed as seed
from canopy.mole.adapters.registry import build_registry
from canopy.mole.adapters.osprey_mongodb import (
    OspreyMongoRecorderAdapter,
    map_mongo_error,
)
from tests.canopy.mole.adapters.conftest import PASSWORD_ENV

COLLECTION = "pv_history"
PV_A = "BTS:DIAG:BPM:01:POSITION:X"
PV_B = "BTS:DIAG:BPM:02:POSITION:X"
PV_C = "BTS:DIAG:BPM:03:POSITION:X"

#: A URL nothing listens on; a misconfigured adapter must never get that far.
UNREACHABLE_URL = "mongodb://osprey@127.0.0.1:1/osprey_archive?authSource=admin"


def _cfg(url: str = UNREACHABLE_URL, **overrides) -> ArchiverInstanceConfig:
    extras = overrides.pop("extras", {"collection": COLLECTION})
    return ArchiverInstanceConfig(
        type="osprey_mongodb_recorder",
        url=url,
        api_key_env=overrides.pop("api_key_env", PASSWORD_ENV),
        timeout_s=overrides.pop("timeout_s", 10.0),
        extras=extras,
    )


@pytest.fixture
def forbid_connect(monkeypatch):
    """Fail the test if the adapter ever constructs a client."""

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("a misconfigured adapter must never create a MongoDB client")

    monkeypatch.setattr(osprey_mongodb, "AsyncMongoClient", _boom)


@pytest.fixture
async def make_adapter():
    """Build adapters and close every one of them at teardown."""
    made: list[OspreyMongoRecorderAdapter] = []

    def make(cfg: ArchiverInstanceConfig, name: str = "osprey") -> OspreyMongoRecorderAdapter:
        adapter = OspreyMongoRecorderAdapter.from_config(name, cfg)
        made.append(adapter)
        return adapter

    yield make
    for adapter in made:
        await adapter.close()


# --------------------------------------------------------------------------- #
# Registration and the Mole env
# --------------------------------------------------------------------------- #


def test_registered_under_short_name():
    assert get_registered_type("osprey_mongodb_recorder") is OspreyMongoRecorderAdapter
    assert OspreyMongoRecorderAdapter.type_name == "osprey_mongodb_recorder"


PROPOSAL_ENV = {
    "CANOPY_MOLE__DEFAULT_ARCHIVER": "osprey",
    "CANOPY_MOLE__ARCHIVERS__OSPREY__TYPE": "osprey_mongodb_recorder",
    "CANOPY_MOLE__ARCHIVERS__OSPREY__URL": ("mongodb://osprey@archiver-mongodb:27017/osprey_archive?authSource=admin"),
    "CANOPY_MOLE__ARCHIVERS__OSPREY__EXTRAS__COLLECTION": "pv_history",
    "CANOPY_MOLE__ARCHIVERS__OSPREY__API_KEY_ENV": "MONGO_ROOT_PASSWORD",
    "CANOPY_MOLE__ARCHIVERS__OSPREY__TIMEOUT_S": "10",
}


def _load_proposal_env(monkeypatch, tmp_path, **override) -> MoleConfig:
    monkeypatch.chdir(tmp_path)  # no stray .env file leaks in
    for key, value in {**PROPOSAL_ENV, **override}.items():
        monkeypatch.setenv(key, value)
    return get_mole_config()


async def test_proposal_env_loads_through_get_mole_config(monkeypatch, tmp_path, forbid_connect):
    monkeypatch.setenv("MONGO_ROOT_PASSWORD", "s3cret")
    config = _load_proposal_env(monkeypatch, tmp_path)
    assert config.default_archiver == "osprey"
    cfg = config.archivers["osprey"]
    assert cfg.type == "osprey_mongodb_recorder"
    assert cfg.api_key_env == "MONGO_ROOT_PASSWORD"
    assert cfg.timeout_s == 10.0
    assert cfg.extras == {"collection": "pv_history"}

    registry = build_registry(config)  # boots with Mongo absent: no connect
    adapter = registry.get("osprey")
    assert isinstance(adapter, OspreyMongoRecorderAdapter)
    assert adapter.database == "osprey_archive"
    assert adapter.collection_name == "pv_history"
    assert adapter.config_error is None
    assert adapter.client_options() == {
        "password": "s3cret",
        "serverSelectionTimeoutMS": 2000,
        "timeoutMS": 10000,
        "tz_aware": True,
    }
    assert adapter.caps == {
        "max_raw": 50_000,
        "max_binned_scan": 1_000_000,
        "last_before_lookback_s": 86_400,
    }
    assert adapter._client is None
    await adapter.close()


async def test_env_string_caps_are_coerced_to_int(monkeypatch, tmp_path, forbid_connect):
    monkeypatch.setenv("MONGO_ROOT_PASSWORD", "s3cret")
    config = _load_proposal_env(
        monkeypatch,
        tmp_path,
        CANOPY_MOLE__ARCHIVERS__OSPREY__EXTRAS__MAX_RAW="1200",
        CANOPY_MOLE__ARCHIVERS__OSPREY__EXTRAS__MAX_BINNED_SCAN="5000",
        CANOPY_MOLE__ARCHIVERS__OSPREY__EXTRAS__LAST_BEFORE_LOOKBACK_S="60",
    )
    adapter = OspreyMongoRecorderAdapter.from_config("osprey", config.archivers["osprey"])
    assert adapter.config_error is None
    assert adapter.caps == {"max_raw": 1200, "max_binned_scan": 5000, "last_before_lookback_s": 60}


# --------------------------------------------------------------------------- #
# Misconfiguration: unhealthy, never connects, every operation 503s
# --------------------------------------------------------------------------- #


MISCONFIGS = [
    pytest.param({"api_key_env": "CANOPY_TEST_UNSET_PW"}, "password", id="password-unset"),
    pytest.param({"api_key_env": "CANOPY_TEST_EMPTY_PW"}, "password", id="password-empty"),
    pytest.param({"api_key_env": None}, "api_key_env", id="no-api-key-env"),
    pytest.param({"url": "mongodb://osprey@127.0.0.1:1/?authSource=admin"}, "database", id="no-database"),
    pytest.param({"url": "http://127.0.0.1:1/osprey_archive"}, "URI", id="bad-uri"),
    pytest.param({"extras": {}}, "collection", id="no-collection"),
    pytest.param({"extras": {"collection": ""}}, "collection", id="empty-collection"),
    pytest.param({"extras": {"collection": COLLECTION, "max_raw": "lots"}}, "max_raw", id="cap-non-int"),
    pytest.param(
        {"extras": {"collection": COLLECTION, "max_binned_scan": "1.5"}},
        "max_binned_scan",
        id="cap-float-string",
    ),
    pytest.param(
        {"extras": {"collection": COLLECTION, "last_before_lookback_s": None}},
        "last_before_lookback_s",
        id="cap-none",
    ),
    pytest.param({"extras": {"collection": COLLECTION, "max_raw": "0"}}, "max_raw", id="cap-not-positive"),
]


@pytest.mark.parametrize(("overrides", "mentions"), MISCONFIGS)
async def test_misconfigured_adapter_is_unhealthy_without_connecting(
    overrides, mentions, monkeypatch, forbid_connect, make_adapter
):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    monkeypatch.delenv("CANOPY_TEST_UNSET_PW", raising=False)
    monkeypatch.setenv("CANOPY_TEST_EMPTY_PW", "")
    adapter = make_adapter(_cfg(**overrides))

    assert adapter.config_error is not None
    assert mentions in adapter.config_error

    health = await asyncio.wait_for(adapter.health(), 0.5)
    assert isinstance(health, AdapterHealth)
    assert health.healthy is False
    assert health.last_error == adapter.config_error
    assert health.last_check_ts_ns > 0

    operations = [
        adapter.fetch_window(PV_A, 0, 1),
        adapter.fetch_binned(PV_A, 0, 1, binning="auto", target_buckets=10, bin_size_ns=None),
        adapter.fetch_last_before(PV_A, 1),
        adapter.metadata(PV_A),
        adapter.search("BTS:*", limit=10),
    ]
    for op in operations:
        with pytest.raises(ArchiverUnavailable) as info:
            await op
        assert type(info.value) is ArchiverUnavailable
        assert info.value.status_code == 503
        assert info.value.detail == health.last_error
    assert adapter._client is None


async def test_password_value_never_appears_in_messages(monkeypatch, forbid_connect, make_adapter):
    monkeypatch.setenv(PASSWORD_ENV, "hunter2-very-secret")
    adapter = make_adapter(_cfg(extras={}))
    assert "hunter2" not in (adapter.config_error or "")
    assert "hunter2" not in repr(adapter)


async def test_close_is_idempotent_without_a_client(forbid_connect, make_adapter, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())
    await adapter.close()
    await adapter.close()
    assert adapter._client is None


# --------------------------------------------------------------------------- #
# FR9 error mapping, in its stated order
# --------------------------------------------------------------------------- #


class _TimeoutFlagged(mongo_errors.PyMongoError):
    @property
    def timeout(self) -> bool:
        return True


FR9_TABLE = [
    # (1) server selection timed out -> 503 (even though .timeout is True)
    pytest.param(
        mongo_errors.ServerSelectionTimeoutError("no servers"), ArchiverUnavailable, 503, id="1-server-selection"
    ),
    # (2) timeouts -> 504
    pytest.param(mongo_errors.NetworkTimeout("socket timed out"), ArchiverTimeout, 504, id="2-network-timeout"),
    pytest.param(mongo_errors.ExecutionTimeout("maxTimeMS", code=50), ArchiverTimeout, 504, id="2-execution-timeout"),
    pytest.param(TimeoutError(), ArchiverTimeout, 504, id="2-asyncio-timeout"),
    pytest.param(_TimeoutFlagged("csot"), ArchiverTimeout, 504, id="2-timeout-flag"),
    # (3) bare AutoReconnect -> 503
    pytest.param(mongo_errors.AutoReconnect("connection reset"), ArchiverUnavailable, 503, id="3-autoreconnect"),
    # (4) unauthorized / auth failed -> 503
    pytest.param(
        mongo_errors.OperationFailure("not authorized", code=13), ArchiverUnavailable, 503, id="4-unauthorized"
    ),
    pytest.param(mongo_errors.OperationFailure("auth failed", code=18), ArchiverUnavailable, 503, id="4-auth-failed"),
    # (5) anything else -> 502
    pytest.param(
        mongo_errors.OperationFailure("bad query", code=2), ArchiverBadResponse, 502, id="5-operation-failure"
    ),
    pytest.param(mongo_errors.PyMongoError("weird"), ArchiverBadResponse, 502, id="5-pymongo"),
    pytest.param(mongo_errors.ConnectionFailure("refused"), ArchiverBadResponse, 502, id="5-connection-failure"),
]


@pytest.mark.parametrize(("exc", "expected_type", "status"), FR9_TABLE)
def test_fr9_error_mapping_order(exc, expected_type, status):
    mapped = map_mongo_error(exc, "osprey")
    assert type(mapped) is expected_type
    assert mapped.status_code == status
    assert "osprey" in mapped.detail


@pytest.mark.parametrize(("exc", "expected_type", "status"), FR9_TABLE)
async def test_fr9_errors_surface_through_operations(exc, expected_type, status, monkeypatch, make_adapter):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())

    async def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(adapter, "_query_channel_index", _raise)
    with pytest.raises(expected_type) as info:
        await adapter.fetch_window(PV_A, 0, 1)
    assert type(info.value) is expected_type
    assert info.value.status_code == status
    assert info.value.__cause__ is exc


async def test_health_reports_mapped_error_message(monkeypatch, make_adapter):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())

    async def _ping():
        raise mongo_errors.OperationFailure("Authentication failed.", code=18)

    monkeypatch.setattr(adapter, "_ping", _ping)
    health = await adapter.health()
    assert health.healthy is False
    assert "Authentication failed" in (health.last_error or "")


async def test_health_ping_is_bounded_below_registry_window(monkeypatch, make_adapter):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())

    async def _hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(adapter, "_ping", _hang)
    started = asyncio.get_running_loop().time()
    health = await asyncio.wait_for(adapter.health(), 2.0)
    elapsed = asyncio.get_running_loop().time() - started
    assert health.healthy is False
    assert elapsed < 1.9
    assert "1.5" in (health.last_error or "")
    assert health.last_error != "health probe timed out"


async def test_unreachable_server_message_beats_registry_timeout(monkeypatch, make_adapter):
    """A real dead port: the adapter answers before the registry's 2 s window."""
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())
    health = await asyncio.wait_for(adapter.health(), 2.0)
    assert health.healthy is False
    assert health.last_error


# --------------------------------------------------------------------------- #
# Channel names rejected before any lookup
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pv",
    ["BTS:BPM.X", "$where", "$gt", "_id", "date", "expireAt", "osprey_densified", ""],
)
async def test_invalid_or_reserved_names_raise_pv_not_found(pv, monkeypatch, make_adapter):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    adapter = make_adapter(_cfg())

    async def _no_lookup(*args, **kwargs):  # pragma: no cover - regression guard
        raise AssertionError("an invalid name must be rejected before any query")

    monkeypatch.setattr(adapter, "_query_channel_index", _no_lookup)
    for op in (adapter.fetch_window(pv, 0, 1), adapter.fetch_last_before(pv, 1), adapter.metadata(pv)):
        with pytest.raises(PVNotFoundInArchiver) as info:
            await op
        assert info.value.status_code == 404
        assert info.value.error_code == "pv_not_found_in_archiver"
    assert adapter._client is None


# --------------------------------------------------------------------------- #
# Channel index against a real (throwaway) MongoDB
# --------------------------------------------------------------------------- #


def _now_slot() -> datetime:
    return seed.utc_slot(datetime.now(UTC).timestamp())


@pytest.mark.mongo
async def test_index_is_union_of_oldest_and_newest_minus_exclusions(
    mongo_url, mongo_client, mongo_db_name, make_adapter
):
    coll = mongo_client[mongo_db_name][COLLECTION]
    seed.prepare_collection(coll)
    now = _now_slot()
    seed.seed_samples(
        coll,
        [
            (now - timedelta(minutes=10), {PV_A: 1.0}),
            (now - timedelta(minutes=5), {PV_C: 3.0}),  # neither oldest nor newest
            (now, {PV_B: 2.0}),
        ],
    )
    seed.write_manifest(coll)  # undated: must not leak `fingerprint`, `seeded_at`, ...
    # A densified doc: its marker field is not a channel.
    coll.insert_one({"date": now - timedelta(minutes=20), PV_A: 0.5, "osprey_densified": True})

    adapter = make_adapter(_cfg(mongo_url))
    index = await adapter.channel_index()
    assert index == frozenset({PV_A, PV_B})
    for field in ("_id", "date", "expireAt", "osprey_densified", "fingerprint", "seeded_at", "touched_windows"):
        assert field not in index

    # Membership check passes for an indexed channel; nothing was recorded at the epoch.
    assert await adapter.fetch_window(PV_A, 0, 1) == []
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window(PV_C, 0, 1)
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.metadata("fingerprint")

    health = await adapter.health()
    assert health.healthy is True
    assert health.last_error is None
    assert health.latency_ms is not None


@pytest.mark.mongo
async def test_densified_newest_doc_marker_stays_out(mongo_url, mongo_client, mongo_db_name, make_adapter):
    coll = mongo_client[mongo_db_name][COLLECTION]
    now = _now_slot()
    coll.insert_one({"date": now, PV_A: 1.0, "osprey_densified": True})
    adapter = make_adapter(_cfg(mongo_url))
    assert await adapter.channel_index() == frozenset({PV_A})


@pytest.mark.mongo
async def test_index_is_cached_for_thirty_seconds(mongo_url, mongo_client, mongo_db_name, make_adapter, monkeypatch):
    coll = mongo_client[mongo_db_name][COLLECTION]
    now = _now_slot()
    seed.write_sample(coll, now, {PV_A: 1.0})
    adapter = make_adapter(_cfg(mongo_url))

    clock = [1000.0]
    monkeypatch.setattr(osprey_mongodb.time, "monotonic", lambda: clock[0])
    assert await adapter.channel_index() == frozenset({PV_A})

    seed.write_sample(coll, now + timedelta(seconds=1), {PV_B: 2.0})
    clock[0] += 29.0
    assert await adapter.channel_index() == frozenset({PV_A})
    clock[0] += 2.0
    assert await adapter.channel_index() == frozenset({PV_A, PV_B})


@pytest.mark.mongo
async def test_empty_collection_is_unhealthy_with_message(mongo_url, mongo_client, mongo_db_name, make_adapter):
    coll = mongo_client[mongo_db_name][COLLECTION]
    seed.write_manifest(coll)  # only the undated manifest: still "no dated documents"
    adapter = make_adapter(_cfg(mongo_url))
    health = await adapter.health()
    assert health.healthy is False
    assert health.last_error == f"{mongo_db_name}.{COLLECTION} has no dated documents"
    assert await adapter.channel_index() == frozenset()
    with pytest.raises(PVNotFoundInArchiver):
        await adapter.fetch_window(PV_A, 0, 1)


@pytest.mark.mongo
async def test_wrong_password_maps_to_503(mongo_url, make_adapter, monkeypatch):
    monkeypatch.setenv("CANOPY_TEST_WRONG_PW", "definitely-not-it")
    adapter = make_adapter(_cfg(mongo_url, api_key_env="CANOPY_TEST_WRONG_PW"))
    with pytest.raises(ArchiverUnavailable) as info:
        await adapter.fetch_window(PV_A, 0, 1)
    assert type(info.value) is ArchiverUnavailable
    assert info.value.status_code == 503
    health = await adapter.health()
    assert health.healthy is False


@pytest.mark.mongo
async def test_close_is_idempotent_after_connecting(mongo_url, mongo_client, mongo_db_name, make_adapter):
    seed.write_sample(mongo_client[mongo_db_name][COLLECTION], _now_slot(), {PV_A: 1.0})
    adapter = make_adapter(_cfg(mongo_url))
    assert (await adapter.health()).healthy
    assert adapter._client is not None
    await adapter.close()
    assert adapter._client is None
    await adapter.close()
