"""Write archive documents the way OSPREY's archiver-recorder and seeder do.

The Mongo archiver adapter reads a collection that two OSPREY processes write:
the archiver-recorder (``osprey.services.archiver_recorder.store``), which stores
one document per instant, and the scenario seeder
(``osprey_connectors.simulation.archiver_seed``), which also leaves one undated
manifest document in the same collection. Tests that seed through this module
get documents of exactly that shape, so the adapter is tested against what it
will meet in a real deployment rather than against a shape the tests invented:

* the recorder upserts on ``date`` (a timezone-aware UTC ``datetime``, stored as
  a BSON Date on a whole-second grid), ``$set``-ing one float field per PV and
  ``$setOnInsert``-ing ``expireAt``;
* the seeder's manifest has ``_id`` ``osprey:seed_manifest`` and carries no
  ``date`` field, so no window query can match it.

The leading underscore keeps the module out of pytest collection.
"""

from __future__ import annotations

from typing import Any
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping, Iterable

#: Field holding each document's sample instant (OSPREY ``DATE_FIELD``).
DATE_FIELD = "date"

#: TTL field the recorder stamps on insert (OSPREY ``EXPIRE_FIELD``).
EXPIRE_FIELD = "expireAt"

#: ``_id`` of the seeder's undated manifest document (OSPREY ``MANIFEST_ID``).
MANIFEST_ID = "osprey:seed_manifest"

#: Recorder knobs used when a test does not pass its own. Only ``expireAt``
#: depends on them, and no adapter reads ``expireAt``.
TAIL_CADENCE_SEC = 60
HOT_SPAN_HOURS = 24
RETENTION_DAYS = 30


def _require_utc(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"recorder timestamps are timezone-aware UTC datetimes; got {timestamp!r}")


def expiry_for(
    timestamp: datetime,
    *,
    tail_cadence_sec: int = TAIL_CADENCE_SEC,
    hot_span_hours: int = HOT_SPAN_HOURS,
    retention_days: int = RETENTION_DAYS,
) -> datetime:
    """The recorder's two-tier expiry: tail-grid samples keep the full retention."""
    on_tail_grid = int(timestamp.timestamp()) % tail_cadence_sec == 0
    span = timedelta(days=retention_days) if on_tail_grid else timedelta(hours=hot_span_hours)
    return timestamp + span


def utc_slot(epoch_seconds: int | float) -> datetime:
    """The recorder's sample instant for ``epoch_seconds``: whole seconds, UTC."""
    return datetime.fromtimestamp(int(epoch_seconds), tz=UTC)


def prepare_collection(collection: Any) -> None:
    """Create the indexes the seeder creates: ``date`` and the ``expireAt`` TTL."""
    collection.create_index(DATE_FIELD)
    collection.create_index(EXPIRE_FIELD, expireAfterSeconds=0)


def write_sample(
    collection: Any,
    timestamp: datetime,
    values: Mapping[str, float],
    **expiry_knobs: int,
) -> None:
    """Store one instant's readings exactly as ``ArchiveWriter.write_sample`` does."""
    _require_utc(timestamp)
    if not values:
        return
    collection.update_one(
        {DATE_FIELD: timestamp},
        {
            "$set": {name: float(value) for name, value in values.items()},
            "$setOnInsert": {EXPIRE_FIELD: expiry_for(timestamp, **expiry_knobs)},
        },
        upsert=True,
    )


def seed_samples(
    collection: Any,
    samples: Iterable[tuple[datetime, Mapping[str, float]]],
    **expiry_knobs: int,
) -> int:
    """Write each ``(timestamp, {pv: value})`` pair as the recorder would; return the count."""
    count = 0
    for timestamp, values in samples:
        write_sample(collection, timestamp, values, **expiry_knobs)
        count += 1
    return count


def write_manifest(
    collection: Any,
    fingerprint: Mapping[str, Any] | None = None,
    *,
    seeded_at: datetime | None = None,
) -> None:
    """Add the seeder's undated manifest document, replacing any previous one."""
    document = {
        "_id": MANIFEST_ID,
        "fingerprint": dict(fingerprint or {"source": "canopy-tests"}),
        "seeded_at": seeded_at or datetime.now(UTC),
        "touched_windows": [],
    }
    collection.replace_one({"_id": MANIFEST_ID}, document, upsert=True)
