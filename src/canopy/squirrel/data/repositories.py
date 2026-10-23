"""Squirrel repositories — thin data-access objects over ``core.db`` sessions.

Each repository takes an ``AsyncSession`` and only ever ``flush()``es — the transaction boundary
(commit / rollback) is owned by the caller (``get_session`` request dep, or ``session_scope``
in a worker task). The cache-path ``bulk_create_fast`` (separate-pool COPY) is
dropped; ``create_snapshot`` uses ``canopy.squirrel.services.bulk_insert`` for the
atomic fast path and ``SnapshotValueRepository.bulk_create`` is the ORM fallback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from collections.abc import Callable, Awaitable

from sqlalchemy import (
    Select,
    or_,
    any_,
    func,
    delete,
    insert,
    select,
    tuple_,
    update,
    literal,
)
from sqlalchemy.orm import noload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.db import BaseRepository
from canopy.squirrel.data.models import (
    Pv,
    Tag,
    MetaPv,
    Snapshot,
    TagGroup,
    SnapshotValue,
    pv_tag,
)

# Chunk size for ORM bulk inserts — keeps a large insert from monopolizing the
# event loop and gives progress a cadence.
BULK_INSERT_CHUNK_SIZE = 5000

# Postgres caps a statement at 32767 bind parameters; stay well under it when
# expanding an ``IN (...)`` / address list.
PARAM_BATCH_SIZE = 30000


class SnapshotRepository(BaseRepository[Snapshot]):
    """Repository for Snapshot operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Snapshot, session)

    async def get_with_values(
        self, snapshot_id: str, limit: int | None = None, offset: int = 0
    ) -> tuple[Snapshot, list[SnapshotValue]] | None:
        """Get a snapshot plus a paginated, name-ordered page of its values.

        The page is returned as a standalone list, never assigned onto the mapped
        ``Snapshot.values`` collection: that relationship is ``cascade="all,
        delete-orphan"``, so assigning a partial page to it would mark the off-page rows
        as orphans and DELETE them on the next commit. ``noload`` also skips the
        relationship's selectin default so a huge snapshot isn't fully loaded just to page it.
        """
        result = await self.session.execute(
            select(Snapshot).where(Snapshot.id == snapshot_id).options(noload(Snapshot.values))
        )
        snapshot = result.scalar_one_or_none()
        if not snapshot:
            return None

        values_query = (
            select(SnapshotValue)
            .where(SnapshotValue.snapshot_id == snapshot_id)
            .order_by(SnapshotValue.pv_name)
            .offset(offset)
        )
        if limit is not None:
            values_query = values_query.limit(limit)

        values_result = await self.session.execute(values_query)
        return snapshot, list(values_result.scalars().all())

    async def update_metadata(
        self,
        snapshot_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> Snapshot | None:
        """Update a snapshot's title and/or description."""
        result = await self.session.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
        snapshot = result.scalar_one_or_none()
        if not snapshot:
            return None

        if title is not None:
            snapshot.title = title
        if description is not None:
            snapshot.description = description

        await self.session.flush()
        return snapshot

    @staticmethod
    def _filtered(
        query: Select,
        title: str | None,
        tag_ids: list[str] | None,
        start: datetime | None,
        end: datetime | None,
    ) -> Select:
        """Apply the snapshot listing filters to ``query``; shared by the page and count queries."""
        if title:
            query = query.where(Snapshot.title.ilike(f"%{title}%"))

        if tag_ids:
            snapshot_ids_with_tags = (
                select(SnapshotValue.snapshot_id)
                .distinct()
                .join(pv_tag, SnapshotValue.pv_id == pv_tag.c.pv_id)
                .where(pv_tag.c.tag_id.in_(tag_ids))
            )
            query = query.where(Snapshot.id.in_(snapshot_ids_with_tags))

        if start is not None:
            query = query.where(Snapshot.created_at >= start)

        if end is not None:
            query = query.where(Snapshot.created_at <= end)

        return query

    async def search(
        self,
        title: str | None = None,
        tag_ids: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[list[Snapshot], tuple[datetime, str] | None, int]:
        """Keyset-paginated search by title, tags and/or creation time.

        Returns ``(results, next_cursor, total_count)``, newest first. A page starts after
        a ``(created_at, id)`` cursor rather than an offset, so snapshots created or deleted
        between page requests cannot shift later pages into repeating or skipping a row.
        ``id`` breaks ties between equal ``created_at`` values (bulk imports can share a
        capture time). The cursor is a sort position, not a row reference, so it stays
        valid even when the snapshot it came from has been deleted.

        Parameters
        ----------
        title : str | None
            Case-insensitive substring match against the snapshot title.
        tag_ids : list[str] | None
            Tag IDs; a snapshot matches when it holds a PV carrying any of them.
        start : datetime | None
            Lower bound on ``created_at``, inclusive.
        end : datetime | None
            Upper bound on ``created_at``, inclusive.
        limit : int
            Maximum number of rows returned.
        after : tuple[datetime, str] | None
            ``(created_at, id)`` of the last row of the previous page; ``None`` for the first.

        Returns
        -------
        tuple[list[Snapshot], tuple[datetime, str] | None, int]
            The page ordered by ``created_at`` then ``id``, both descending; the cursor for
            the next page, or ``None`` on the last page; and the count of all matching rows.
        """
        # ``noload`` skips the relationship's selectin default: listing needs only snapshot
        # rows, and loading every value for every listed snapshot (~16k each for a full
        # machine save) dominated the response time.
        query = self._filtered(select(Snapshot).options(noload(Snapshot.values)), title, tag_ids, start, end)
        count_query = self._filtered(select(func.count()).select_from(Snapshot), title, tag_ids, start, end)

        if after is not None:
            query = query.where(
                tuple_(Snapshot.created_at, Snapshot.id)
                < tuple_(
                    # Typed like the columns: a bare string binds as VARCHAR, and
                    # Postgres has no ``uuid < varchar`` operator.
                    literal(after[0], Snapshot.created_at.type),
                    literal(after[1], Snapshot.id.type),
                )
            )

        # +1 to detect a next page
        query = query.order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(limit + 1)

        result = await self.session.execute(query)
        snapshots = list(result.scalars().all())

        count_result = await self.session.execute(count_query)
        total_count = count_result.scalar() or 0

        next_cursor = None
        if len(snapshots) > limit:
            snapshots = snapshots[:limit]
            next_cursor = (snapshots[-1].created_at, snapshots[-1].id)

        return snapshots, next_cursor, total_count

    async def get_value_count(self, snapshot_id: str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(SnapshotValue).where(SnapshotValue.snapshot_id == snapshot_id)
        )
        return result.scalar() or 0

    async def get_value_counts_batch(self, snapshot_ids: list[str]) -> dict[str, int]:
        """Value counts for many snapshots in a single grouped query."""
        if not snapshot_ids:
            return {}
        result = await self.session.execute(
            select(SnapshotValue.snapshot_id, func.count())
            .where(SnapshotValue.snapshot_id.in_(snapshot_ids))
            .group_by(SnapshotValue.snapshot_id)
        )
        return {row[0]: row[1] for row in result.all()}

    async def delete_with_values(self, snapshot_id: str) -> bool:
        """Delete a snapshot; its values go via the DB ``ON DELETE CASCADE``."""
        result = await self.session.execute(select(Snapshot.id).where(Snapshot.id == snapshot_id))
        if not result.scalar_one_or_none():
            return False

        await self.session.execute(delete(Snapshot).where(Snapshot.id == snapshot_id))
        await self.session.flush()
        return True


class SnapshotValueRepository(BaseRepository[SnapshotValue]):
    """Repository for SnapshotValue operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(SnapshotValue, session)

    async def bulk_create(
        self,
        values: list[SnapshotValue],
        progress_callback: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> None:
        """ORM chunked insert (the fallback path; ``services.bulk_insert`` is the fast path).

        Inserts in chunks of ``BULK_INSERT_CHUNK_SIZE`` and yields to the event loop
        between chunks so a large insert doesn't stall other work.
        """
        total = len(values)

        if total <= BULK_INSERT_CHUNK_SIZE:
            self.session.add_all(values)
            await self.session.flush()
            await asyncio.sleep(0)  # Yield to event loop
            return

        for i in range(0, total, BULK_INSERT_CHUNK_SIZE):
            chunk = values[i : i + BULK_INSERT_CHUNK_SIZE]
            self.session.add_all(chunk)
            await self.session.flush()
            await asyncio.sleep(0)
            if progress_callback:
                current = min(i + BULK_INSERT_CHUNK_SIZE, total)
                await progress_callback(current, total, f"Saved {current}/{total} values...")

    async def get_by_snapshot(self, snapshot_id: str) -> list[SnapshotValue]:
        result = await self.session.execute(select(SnapshotValue).where(SnapshotValue.snapshot_id == snapshot_id))
        return list(result.scalars().all())

    async def count_by_snapshot(self, snapshot_id: str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(SnapshotValue).where(SnapshotValue.snapshot_id == snapshot_id)
        )
        return result.scalar() or 0

    async def get_by_snapshot_and_pvs(self, snapshot_id: str, pv_ids: list[str]) -> list[SnapshotValue]:
        """Get specific PV values from a snapshot (``= ANY(array)`` avoids huge IN expansion)."""
        result = await self.session.execute(
            select(SnapshotValue).where(SnapshotValue.snapshot_id == snapshot_id, SnapshotValue.pv_id == any_(pv_ids))
        )
        return list(result.scalars().all())


class PvRepository(BaseRepository[Pv]):
    """Repository for PV operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Pv, session)

    async def find_by_address(self, address: str) -> Pv | None:
        """Find a PV by any of its addresses (setpoint, readback, or config)."""
        result = await self.session.execute(
            select(Pv)
            .options(selectinload(Pv.tags).selectinload(Tag.group))
            .where(or_(Pv.setpoint_address == address, Pv.readback_address == address, Pv.config_address == address))
        )
        return result.scalar_one_or_none()

    async def find_by_setpoint(self, setpoint_address: str) -> Pv | None:
        result = await self.session.execute(
            select(Pv)
            .options(selectinload(Pv.tags).selectinload(Tag.group))
            .where(Pv.setpoint_address == setpoint_address)
        )
        return result.scalar_one_or_none()

    async def find_by_readback_without_setpoint(self, readback_address: str) -> Pv | None:
        """Find a setpoint-less PV by its readback address.

        Parameters
        ----------
        readback_address : str
            Readback address to match.

        Returns
        -------
        Pv | None
            A PV whose ``setpoint_address`` is NULL and whose ``readback_address`` equals
            ``readback_address``, or ``None`` when no such PV exists.
        """
        result = await self.session.execute(
            select(Pv)
            .options(selectinload(Pv.tags).selectinload(Tag.group))
            .where(Pv.setpoint_address.is_(None), Pv.readback_address == readback_address)
            .limit(1)
        )
        return result.scalars().first()

    async def find_by_config_only(self, config_address: str) -> Pv | None:
        """Find a config-only PV by its config address.

        Parameters
        ----------
        config_address : str
            Config address to match.

        Returns
        -------
        Pv | None
            A PV whose ``setpoint_address`` and ``readback_address`` are both NULL and whose
            ``config_address`` equals ``config_address``, or ``None`` when no such PV exists.
        """
        result = await self.session.execute(
            select(Pv)
            .options(selectinload(Pv.tags).selectinload(Tag.group))
            .where(
                Pv.setpoint_address.is_(None),
                Pv.readback_address.is_(None),
                Pv.config_address == config_address,
            )
            .limit(1)
        )
        return result.scalars().first()

    async def get_existing_setpoints(self, setpoint_addresses: list[str]) -> set[str]:
        """Return the subset of ``setpoint_addresses`` that already exist.

        Parameters
        ----------
        setpoint_addresses : list[str]
            Setpoint addresses to look up.

        Returns
        -------
        set[str]
            The addresses already registered as a PV setpoint.
        """
        if not setpoint_addresses:
            return set()
        existing: set[str] = set()
        for i in range(0, len(setpoint_addresses), PARAM_BATCH_SIZE):
            batch = setpoint_addresses[i : i + PARAM_BATCH_SIZE]
            result = await self.session.execute(select(Pv.setpoint_address).where(Pv.setpoint_address.in_(batch)))
            existing.update({row[0] for row in result.all() if row[0]})
        return existing

    async def get_existing_readbacks_without_setpoint(self, readback_addresses: list[str]) -> set[str]:
        """Return the subset of ``readback_addresses`` already held by a setpoint-less PV.

        Parameters
        ----------
        readback_addresses : list[str]
            Readback addresses to look up.

        Returns
        -------
        set[str]
            The addresses carried by an existing PV whose ``setpoint_address`` is NULL.
        """
        if not readback_addresses:
            return set()
        existing: set[str] = set()
        for i in range(0, len(readback_addresses), PARAM_BATCH_SIZE):
            batch = readback_addresses[i : i + PARAM_BATCH_SIZE]
            result = await self.session.execute(
                select(Pv.readback_address).where(Pv.setpoint_address.is_(None), Pv.readback_address.in_(batch))
            )
            existing.update({row[0] for row in result.all() if row[0]})
        return existing

    async def get_existing_config_only(self, config_addresses: list[str]) -> set[str]:
        """Return the subset of ``config_addresses`` already held by a config-only PV.

        Parameters
        ----------
        config_addresses : list[str]
            Config addresses to look up.

        Returns
        -------
        set[str]
            The addresses carried by an existing PV whose ``setpoint_address`` and
            ``readback_address`` are both NULL.
        """
        if not config_addresses:
            return set()
        existing: set[str] = set()
        for i in range(0, len(config_addresses), PARAM_BATCH_SIZE):
            batch = config_addresses[i : i + PARAM_BATCH_SIZE]
            result = await self.session.execute(
                select(Pv.config_address).where(
                    Pv.setpoint_address.is_(None),
                    Pv.readback_address.is_(None),
                    Pv.config_address.in_(batch),
                )
            )
            existing.update({row[0] for row in result.all() if row[0]})
        return existing

    async def search_by_name(
        self,
        search: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
        tag_filters: dict[str, list[str]] | None = None,
    ) -> tuple[list[Pv], str | None, int]:
        """Keyset-paginated PV search. Returns ``(results, next_token, total_count)``.

        ``tag_filters`` is OR within a group, AND across groups.
        """
        query = select(Pv).options(selectinload(Pv.tags).selectinload(Tag.group))
        count_query = select(func.count()).select_from(Pv)

        if search:
            search_filter = or_(
                Pv.setpoint_address.ilike(f"%{search}%"),
                Pv.readback_address.ilike(f"%{search}%"),
                Pv.config_address.ilike(f"%{search}%"),
                Pv.device.ilike(f"%{search}%"),
                Pv.description.ilike(f"%{search}%"),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        if tag_filters:
            for _group_id, tag_ids in tag_filters.items():
                if tag_ids:
                    subquery = select(pv_tag.c.pv_id).where(pv_tag.c.tag_id.in_(tag_ids)).distinct()
                    query = query.where(Pv.id.in_(subquery))
                    count_query = count_query.where(Pv.id.in_(subquery))

        if continuation_token:
            query = query.where(Pv.id > continuation_token)

        query = query.order_by(Pv.id).limit(limit + 1)  # +1 to detect a next page

        result = await self.session.execute(query)
        pvs = list(result.scalars().all())

        count_result = await self.session.execute(count_query)
        total_count = count_result.scalar() or 0

        next_token = None
        if len(pvs) > limit:
            pvs = pvs[:limit]
            next_token = pvs[-1].id

        return pvs, next_token, total_count

    async def get_all_addresses(self) -> list[tuple[str, str | None, str | None, str | None]]:
        """All ``(id, setpoint, readback, config)`` tuples — the snapshot-capture input."""
        result = await self.session.execute(select(Pv.id, Pv.setpoint_address, Pv.readback_address, Pv.config_address))
        return list(result.all())

    async def bulk_create(self, pvs: list[Pv]) -> list[Pv]:
        self.session.add_all(pvs)
        await self.session.flush()
        for pv in pvs:
            await self.session.refresh(pv)
        return pvs

    async def get_by_ids(self, ids: list[str]) -> list[Pv]:
        """Get many PVs by id, batched under the Postgres parameter limit."""
        if not ids:
            return []
        all_pvs: list[Pv] = []
        for i in range(0, len(ids), PARAM_BATCH_SIZE):
            batch_ids = ids[i : i + PARAM_BATCH_SIZE]
            result = await self.session.execute(
                select(Pv).options(selectinload(Pv.tags).selectinload(Tag.group)).where(Pv.id.in_(batch_ids))
            )
            all_pvs.extend(result.scalars().all())
        return all_pvs

    async def search_filtered(
        self,
        search_term: str | None = None,
        devices: list[str] | None = None,
        tag_ids: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Pv], int]:
        """Offset-paginated, server-side filtered search. Returns ``(results, total_count)``."""
        query = select(Pv).options(selectinload(Pv.tags).selectinload(Tag.group))
        count_query = select(func.count()).select_from(Pv)

        if search_term:
            search_filter = or_(
                Pv.setpoint_address.ilike(f"%{search_term}%"),
                Pv.readback_address.ilike(f"%{search_term}%"),
                Pv.config_address.ilike(f"%{search_term}%"),
                Pv.device.ilike(f"%{search_term}%"),
                Pv.description.ilike(f"%{search_term}%"),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        if devices:
            query = query.where(Pv.device.in_(devices))
            count_query = count_query.where(Pv.device.in_(devices))

        if tag_ids:
            # Match PVs carrying ANY of the tags via a subquery rather than a join: a join
            # multiplies a PV row per matching tag, duplicating results and inflating
            # total_count (which corrupts offset pagination). Mirrors ``search_by_name``.
            pvs_with_tags = select(pv_tag.c.pv_id).where(pv_tag.c.tag_id.in_(tag_ids)).distinct()
            query = query.where(Pv.id.in_(pvs_with_tags))
            count_query = count_query.where(Pv.id.in_(pvs_with_tags))

        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        query = query.order_by(Pv.setpoint_address).offset(offset).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all()), total

    async def get_all_devices(self) -> list[str]:
        result = await self.session.execute(
            select(Pv.device).where(Pv.device.isnot(None)).distinct().order_by(Pv.device)
        )
        return [r[0] for r in result.all() if r[0]]

    async def get_all_as_map(self) -> dict[str, Pv]:
        result = await self.session.execute(select(Pv).options(selectinload(Pv.tags).selectinload(Tag.group)))
        pvs = result.scalars().all()
        return {pv.id: pv for pv in pvs}


class TagGroupRepository(BaseRepository[TagGroup]):
    """Repository for TagGroup operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(TagGroup, session)

    async def get_with_tags(self, group_id: str) -> TagGroup | None:
        result = await self.session.execute(
            select(TagGroup)
            .options(selectinload(TagGroup.tags))
            .where(TagGroup.id == group_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def find_by_name(self, name: str) -> TagGroup | None:
        result = await self.session.execute(select(TagGroup).where(func.lower(TagGroup.name) == name.lower()))
        return result.scalar_one_or_none()

    async def get_all_with_counts(self) -> list[tuple[TagGroup, int]]:
        result = await self.session.execute(
            select(TagGroup, func.count(Tag.id).label("tag_count"))
            .outerjoin(Tag)
            .group_by(TagGroup.id)
            .order_by(TagGroup.name)
        )
        return list(result.all())


class TagRepository(BaseRepository[Tag]):
    """Repository for Tag operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(Tag, session)

    async def get_by_ids(self, tag_ids: list[str]) -> list[Tag]:
        if not tag_ids:
            return []
        result = await self.session.execute(select(Tag).where(Tag.id.in_(tag_ids)))
        return list(result.scalars().all())

    async def find_by_group_and_name(self, group_id: str, name: str) -> Tag | None:
        """Find a tag by group + case-insensitive name (the de-facto per-group uniqueness check)."""
        result = await self.session.execute(
            select(Tag).where(Tag.group_id == group_id).where(func.lower(Tag.name) == name.lower())
        )
        return result.scalar_one_or_none()


class MetaPvRepository(BaseRepository[MetaPv]):
    """Repository for meta-PV management (Core insert/update/delete with RETURNING)."""

    def __init__(self, session: AsyncSession):
        super().__init__(MetaPv, session)

    async def get_one(self, id: str) -> MetaPv | None:
        result = await self.session.execute(select(MetaPv).where(MetaPv.pv_id == id))
        return result.scalars().one_or_none()

    async def get(self, active: bool | None = None) -> list[MetaPv]:
        stmt = select(MetaPv)
        if active is not None:
            stmt = stmt.where(MetaPv.is_active == active)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def enable(self, id: str) -> MetaPv | None:
        result = await self.session.execute(
            update(MetaPv).where(MetaPv.pv_id == id).values(is_active=True).returning(MetaPv)
        )
        await self.session.flush()
        return result.scalars().one_or_none()

    async def disable(self, id: str) -> MetaPv | None:
        result = await self.session.execute(
            update(MetaPv).where(MetaPv.pv_id == id).values(is_active=False).returning(MetaPv)
        )
        await self.session.flush()
        return result.scalars().one_or_none()

    async def set_display_name(self, id: str, display_name: str) -> MetaPv | None:
        result = await self.session.execute(
            update(MetaPv).where(MetaPv.pv_id == id).values(display_name=display_name).returning(MetaPv)
        )
        await self.session.flush()
        return result.scalars().one_or_none()

    async def add(self, id: str, display_name: str = "") -> MetaPv | None:
        result = await self.session.execute(
            insert(MetaPv).values(pv_id=id, display_name=display_name, is_active=True).returning(MetaPv)
        )
        await self.session.flush()
        return result.scalars().one_or_none()

    async def delete(self, id: str) -> int:
        result = await self.session.execute(delete(MetaPv).where(MetaPv.pv_id == id))
        await self.session.flush()
        return result.rowcount
