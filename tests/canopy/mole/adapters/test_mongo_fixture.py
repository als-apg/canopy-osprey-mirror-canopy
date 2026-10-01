"""The throwaway-MongoDB fixture and the recorder-shaped seeding helper."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from pymongo import MongoClient

from tests.canopy.mole.adapters import _mongo_seed as seed
from tests.canopy.mole.adapters.conftest import (
    LABEL,
    MONGO_IMAGE,
    OWNER_LABEL,
    PASSWORD_ENV,
    docker,
    reap_stale_mongo_containers,
)

pytestmark = pytest.mark.mongo


def _client_from_url(url: str) -> MongoClient:
    """Connect the way an adapter configured with ``api_key_env`` would."""
    return MongoClient(
        url,
        password=os.environ[PASSWORD_ENV],
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
    )


def test_url_shape_and_password_env(mongo_url, mongo_server):
    parts = urlsplit(mongo_url)
    assert parts.scheme == "mongodb"
    assert parts.username == "osprey"
    assert parts.password is None  # never in the URL
    assert parts.hostname == "127.0.0.1"
    assert parts.port == mongo_server.port
    assert parts.path.lstrip("/").startswith("canopy_test_")
    assert parts.query == "authSource=admin"
    assert os.environ[PASSWORD_ENV] == mongo_server.password


def test_authenticated_insert_and_find_through_url(mongo_url):
    database = urlsplit(mongo_url).path.lstrip("/")
    client = _client_from_url(mongo_url)
    try:
        collection = client.get_default_database()["pv_history"]
        assert collection.database.name == database
        collection.insert_one({"name": "probe", "value": 1.5})
        found = collection.find_one({"name": "probe"}, {"_id": 0})
        assert found == {"name": "probe", "value": 1.5}
    finally:
        client.close()


def test_wrong_password_is_rejected(mongo_url):
    from pymongo.errors import OperationFailure

    client = MongoClient(mongo_url, password="wrong", serverSelectionTimeoutMS=5000)
    try:
        with pytest.raises(OperationFailure):
            client.admin.command("ping")
    finally:
        client.close()


def test_each_test_gets_a_fresh_database(mongo_url, mongo_client):
    database = urlsplit(mongo_url).path.lstrip("/")
    assert database not in mongo_client.list_database_names()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _start_sleeper(owner: str) -> str:
    return docker(
        "run",
        "-d",
        "--rm",
        "--pull",
        "never",
        "--label",
        LABEL,
        "--label",
        f"{OWNER_LABEL}={owner}",
        "--entrypoint",
        "sleep",
        MONGO_IMAGE,
        "300",
    ).stdout.strip()


def _exists(container_id: str) -> bool:
    return docker("inspect", container_id, check=False).returncode == 0


def test_stale_owner_reaped_live_owner_survives(mongo_server):
    stale = _start_sleeper(str(_dead_pid()))
    live = _start_sleeper(str(os.getpid()))
    try:
        reaped = reap_stale_mongo_containers()
        assert any(stale.startswith(r) or r.startswith(stale) for r in reaped)
        assert not _exists(stale)
        assert _exists(live)
        assert _exists(mongo_server.container_id)
    finally:
        docker("rm", "-f", stale, live, check=False)


def test_seeded_date_is_a_bson_date(mongo_client, mongo_db_name, mongo_url):
    collection = mongo_client[mongo_db_name]["pv_history"]
    seed.prepare_collection(collection)
    when = seed.utc_slot(1_790_000_000)
    seed.write_sample(collection, when, {"BTS:DIAG:BPM:01:POSITION:X": 0.25})

    # $type "date" matches BSON type 0x09 (UTC datetime) only, not strings or numbers.
    assert collection.count_documents({"date": {"$type": "date"}}) == 1

    doc = collection.find_one({"date": when})
    assert doc["date"] == when
    assert doc["date"].tzinfo is not None
    assert doc["BTS:DIAG:BPM:01:POSITION:X"] == 0.25
    assert doc["expireAt"] == seed.expiry_for(when)


def test_recorder_upsert_merges_and_keeps_expiry(mongo_client, mongo_db_name, mongo_url):
    collection = mongo_client[mongo_db_name]["pv_history"]
    when = seed.utc_slot(1_790_000_005)  # off the tail grid: hot span
    seed.write_sample(collection, when, {"A": 1.0}, hot_span_hours=1)
    seed.write_sample(collection, when, {"B": 2.0}, hot_span_hours=5)
    docs = list(collection.find({}, {"_id": 0}))
    assert docs == [{"date": when, "A": 1.0, "B": 2.0, "expireAt": when + timedelta(hours=1)}]
    on_grid = seed.utc_slot(1_790_000_040)  # multiple of 60
    assert seed.expiry_for(on_grid) == on_grid + timedelta(days=seed.RETENTION_DAYS)


def test_manifest_is_undated(mongo_client, mongo_db_name, mongo_url):
    collection = mongo_client[mongo_db_name]["pv_history"]
    n = seed.seed_samples(
        collection,
        [(seed.utc_slot(1_790_000_000 + 10 * i), {"PV": float(i)}) for i in range(3)],
    )
    seed.write_manifest(collection, {"preset": "test"})
    assert n == 3
    manifest = collection.find_one({"_id": seed.MANIFEST_ID})
    assert manifest is not None
    assert "date" not in manifest and "expireAt" not in manifest
    assert manifest["touched_windows"] == []
    assert collection.count_documents({"date": {"$exists": True}}) == 3


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError):
        seed.write_sample(object(), datetime(2026, 1, 1), {"PV": 1.0})
    assert seed.utc_slot(1.7).tzinfo is UTC
