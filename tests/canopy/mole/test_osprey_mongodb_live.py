"""OSPREY MongoDB archiver-recorder adapter against the running deployment.

Read-only. Runs only with ``CANOPY_LIVE=1``:

    CANOPY_LIVE=1 CANOPY_TEST_MONGO_ENV_FILE=$MCA/.env \\
        .venv/bin/python -m pytest tests/canopy/mole/test_osprey_mongodb_live.py -ra

Environment:

- ``CANOPY_TEST_MONGO_URL`` -- default
  ``mongodb://osprey@127.0.0.1:10801/osprey_archiver?authSource=admin`` (no password).
- ``CANOPY_TEST_MONGO_COLLECTION`` -- default ``pv_history``.
- ``CANOPY_TEST_MONGO_ENV_FILE`` -- a dotenv file; only its ``MONGO_ROOT_PASSWORD`` is
  read, and the value is never printed or put into a message.

The window is ``[newest - 3600 s, newest]``, anchored on the newest ``date`` among the
documents where the PV exists. Raw and binned reads must count exactly what a direct
``count_documents`` counts, and each read must answer within 500 ms once warm.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import Callable, Awaitable

import pytest
from pymongo import MongoClient

from canopy.mole.config import ArchiverInstanceConfig
from canopy.mole.adapters.osprey_mongodb import (
    DATE_FIELD,
    OspreyMongoRecorderAdapter,
    ns_ceil_ms,
    ns_floor_ms,
    datetime_to_ns,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CANOPY_LIVE") != "1",
        reason="needs the running OSPREY archiver Mongo; set CANOPY_LIVE=1",
    ),
]

DEFAULT_URL = "mongodb://osprey@127.0.0.1:10801/osprey_archiver?authSource=admin"
URL = os.environ.get("CANOPY_TEST_MONGO_URL", DEFAULT_URL)
COLLECTION = os.environ.get("CANOPY_TEST_MONGO_COLLECTION", "pv_history")
ENV_FILE_VAR = "CANOPY_TEST_MONGO_ENV_FILE"
PASSWORD_KEY = "MONGO_ROOT_PASSWORD"
#: Test-private variable the adapter reads the password from (its ``api_key_env``).
PASSWORD_ENV = "CANOPY_TEST_LIVE_MONGO_PW"

PV = "BTS:DIAG:BPM:01:POSITION:X"
WINDOW_S = 3600
BUDGET_S = 0.5
TARGET_BUCKETS = 5000
NS_PER_S = 1_000_000_000


def _read_password() -> str:
    """``MONGO_ROOT_PASSWORD`` from the env file; messages name only the key, never the value."""
    path_str = os.environ.get(ENV_FILE_VAR)
    if not path_str:
        pytest.fail(f"{ENV_FILE_VAR} is not set")
    path = Path(path_str)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        pytest.fail(f"cannot read {ENV_FILE_VAR} file {path}: {type(exc).__name__}")
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != PASSWORD_KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not value:
            pytest.fail(f"{PASSWORD_KEY} is empty in {path}")
        return value
    pytest.fail(f"{PASSWORD_KEY} not found in {path}")


@pytest.fixture(scope="module")
def password() -> str:
    return _read_password()


@pytest.fixture(scope="module")
def coll(password: str):
    client: MongoClient = MongoClient(URL, password=password, tz_aware=True, serverSelectionTimeoutMS=5000)
    try:
        yield client.get_default_database()[COLLECTION]
    finally:
        client.close()


@pytest.fixture(scope="module")
def window(coll) -> tuple[int, int]:
    """``(t0_ns, t1_ns)``: the hour ending at the newest dated document holding the PV."""
    newest = coll.find_one(
        {PV: {"$exists": True}, DATE_FIELD: {"$exists": True}},
        sort=[(DATE_FIELD, -1)],
        projection={DATE_FIELD: 1},
    )
    assert newest is not None, f"no dated document holds {PV} in {COLLECTION}"
    t1_ns = datetime_to_ns(newest[DATE_FIELD])
    return t1_ns - WINDOW_S * NS_PER_S, t1_ns


@pytest.fixture
async def adapter(password: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PASSWORD_ENV, password)
    cfg = ArchiverInstanceConfig(
        type="osprey_mongodb_recorder",
        url=URL,
        api_key_env=PASSWORD_ENV,
        timeout_s=10.0,
        extras={"collection": COLLECTION},
    )
    made = OspreyMongoRecorderAdapter.from_config("osprey-live", cfg)
    assert made.config_error is None, made.config_error
    try:
        health = await made.health()
        assert health.healthy, health.last_error
        yield made
    finally:
        await made.close()


def _reference_count(coll, t0_ns: int, t1_ns: int) -> int:
    return coll.count_documents(
        {
            DATE_FIELD: {"$gte": ns_floor_ms(t0_ns), "$lte": ns_ceil_ms(t1_ns)},
            PV: {"$exists": True},
        }
    )


async def _timed(fetch: Callable[[], Awaitable[int]]) -> tuple[int, float]:
    start = time.perf_counter()
    count = await fetch()
    return count, time.perf_counter() - start


async def _check(coll, window: tuple[int, int], fetch: Callable[[], Awaitable[int]]) -> None:
    """One untimed fetch, then a timed one; count and timing each get one retry."""
    t0_ns, t1_ns = window
    await fetch()  # warm-up, untimed

    count, elapsed = await _timed(fetch)
    if elapsed >= BUDGET_S:
        count, elapsed = await _timed(fetch)
    assert elapsed < BUDGET_S, f"fetch took {elapsed * 1000:.0f} ms (budget {BUDGET_S * 1000:.0f} ms)"

    expected = _reference_count(coll, t0_ns, t1_ns)
    if count != expected:  # a TTL deletion between the two reads: re-count both sides once
        count = await fetch()
        expected = _reference_count(coll, t0_ns, t1_ns)
    assert expected >= 1, f"no {PV} samples in the window"
    assert count == expected


async def test_raw_window_matches_direct_count(adapter, coll, window) -> None:
    t0_ns, t1_ns = window

    async def fetch() -> int:
        return len(await adapter.fetch_window(PV, t0_ns, t1_ns))

    await _check(coll, window, fetch)


async def test_binned_window_matches_direct_count(adapter, coll, window) -> None:
    t0_ns, t1_ns = window

    async def fetch() -> int:
        points = await adapter.fetch_binned(
            PV, t0_ns, t1_ns, binning="auto", target_buckets=TARGET_BUCKETS, bin_size_ns=None
        )
        assert len(points) <= TARGET_BUCKETS
        return sum(p.count for p in points)

    await _check(coll, window, fetch)
