"""Snapshot domain service: create / restore / compare + listing."""

from __future__ import annotations

import json
import math
import logging
from typing import Any
from datetime import UTC, datetime
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.errors import BadRequest
from canopy.core.time_parse import to_utc
from canopy.squirrel.errors import SnapshotNotFound
from canopy.squirrel.data.models import Snapshot
from canopy.squirrel.restore_hooks import (
    RestoreHook,
    RestoreHookError,
    get_restore_hooks,
)
from canopy.squirrel.schemas.snapshot import (
    PVValueDTO,
    TagInfoDTO,
    SnapshotDTO,
    EpicsValueDTO,
    MetaPVValueDTO,
    NewSnapshotDTO,
    RestoreResultDTO,
    RestoreRequestDTO,
    SnapshotSummaryDTO,
    ComparisonResultDTO,
)
from canopy.squirrel.adapters.epics_io import EpicsIO
from canopy.squirrel.data.repositories import (
    PvRepository,
    MetaPvRepository,
    SnapshotRepository,
    SnapshotValueRepository,
)
from canopy.squirrel.services.bulk_insert import bulk_insert_snapshot_values

logger = logging.getLogger(__name__)

PRIMARY_PHASE = "Setpoints"


def _sub_batch_progress(progress_callback: Callable, base: int, total: int) -> Callable:
    """Offset one write batch's progress onto the restore-wide write count.

    Parameters
    ----------
    progress_callback : Callable
        The restore's async progress sink, called as ``(current, total, message)``.
    base : int
        Writes already accounted for when this batch starts.
    total : int
        Every write the restore will attempt.

    Returns
    -------
    Callable
        An async callback in the batch's own scale that reports on the global one.
    """

    async def _report(current: int, _batch_total: int, message: str) -> None:
        await progress_callback(base + current, total, message)

    return _report


def _parse_jsonb(value: Any) -> dict | None:
    """Parse a JSONB column that may come back as a ``str`` or ``dict`` (driver-dependent)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _sanitize_for_json(value: Any) -> Any:
    """Make a value JSON-safe, recursing into lists/dicts.

    ``NaN``/``Inf``/``-Inf`` (invalid JSON) become ``None``; a Pydantic model (e.g. a
    TABLE PV's ``TableValue``) becomes its ``model_dump``. Without the model branch,
    ``json.dumps`` on the COPY path raises ``TypeError`` on a non-scalar PV value and
    aborts the entire snapshot create.
    """
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return _sanitize_for_json(value.model_dump(mode="json"))
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    return value


def _utc_bound(name: str, value: datetime | None) -> datetime | None:
    """Convert a listing bound with ``to_utc``, refusing one that leaves ``datetime``'s range.

    Parameters
    ----------
    name : str
        The query parameter the bound came from, for the error message.
    value : datetime | None
        The bound as the client sent it.

    Returns
    -------
    datetime | None
        The bound as an aware UTC datetime, or ``None`` when it was omitted.

    Raises
    ------
    BadRequest
        When the UTC instant falls outside years 1 to 9999, e.g.
        ``9999-12-31T23:00:00-05:00``.
    """
    if value is None:
        return None
    try:
        return to_utc(value)
    except OverflowError as exc:
        raise BadRequest(f"{name} ({value.isoformat()}) is out of range once converted to UTC") from exc


class SnapshotService:
    """Snapshot operations with EPICS integration. Owns no transaction — the caller
    (request dep or worker ``session_scope``) commits."""

    def __init__(
        self,
        session: AsyncSession,
        epics_io: EpicsIO | None = None,
        restore_hooks: list[RestoreHook] | None = None,
    ):
        """Wire the repositories, EPICS adapter and restore hooks.

        Parameters
        ----------
        session : AsyncSession
            The caller's session; the service owns no transaction.
        epics_io : EpicsIO | None
            Required only for create/restore (run in the worker, which builds the
            connector). The read endpoints construct the service without it.
        restore_hooks : list[RestoreHook] | None
            Hooks to run during a restore. ``None`` falls back to the configured
            singletons; an empty list disables hooks outright, which is how tests
            isolate themselves from the environment.
        """
        self.session = session
        self.snapshot_repo = SnapshotRepository(session)
        self.value_repo = SnapshotValueRepository(session)
        self.pv_repo = PvRepository(session)
        self.meta_pv_repo = MetaPvRepository(session)
        self.epics = epics_io
        self.restore_hooks = restore_hooks if restore_hooks is not None else get_restore_hooks()

    async def _get_meta_pv_values_for_snapshot(self, snapshot_id: str, pv_ids: list[str]) -> list[MetaPVValueDTO]:
        meta_pv_values: list[MetaPVValueDTO] = []
        if pv_ids:
            for value in await self.value_repo.get_by_snapshot_and_pvs(snapshot_id, pv_ids):
                data = _parse_jsonb(value.setpoint_value) or _parse_jsonb(value.readback_value)
                meta_pv_values.append(
                    MetaPVValueDTO(
                        pv_id=value.pv_id,
                        pv_name=value.pv_name,
                        **(data or {"value": None}),
                    )
                )
        return meta_pv_values

    async def list_snapshots(
        self,
        title: str | None = None,
        tag_ids: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[SnapshotSummaryDTO]:
        """List snapshots, optionally filtered by title, tags and/or creation time.

        Parameters
        ----------
        title : str | None
            Case-insensitive substring match against the snapshot title.
        tag_ids : list[str] | None
            Tag IDs; a snapshot matches when it holds a PV carrying any of them.
        start : datetime | None
            Lower bound on creation time, inclusive. Converted with ``to_utc``, because
            ``created_at`` is a ``timestamptz`` and asyncpg refuses a naive bind parameter.
        end : datetime | None
            Upper bound on creation time, inclusive. Converted the same way as ``start``.

        Returns
        -------
        list[SnapshotSummaryDTO]
            Matching snapshot summaries, newest first.

        Raises
        ------
        BadRequest
            When a bound falls outside ``datetime``'s range once converted to UTC, or
            both bounds are given and ``start`` is after ``end``.
        """
        start = _utc_bound("start", start)
        end = _utc_bound("end", end)
        if start is not None and end is not None and start > end:
            raise BadRequest(f"start ({start.isoformat()}) must not be after end ({end.isoformat()})")
        snapshots = await self.snapshot_repo.search(title=title, tag_ids=tag_ids, start=start, end=end)

        # One grouped query for all value counts (avoids N+1).
        snapshot_ids = [snap.id for snap in snapshots]
        counts = await self.snapshot_repo.get_value_counts_batch(snapshot_ids)

        meta_pvs = [pv.pv_id for pv in await self.meta_pv_repo.get(active=True)]
        result = []
        for snap in snapshots:
            result.append(
                SnapshotSummaryDTO(
                    id=snap.id,
                    title=snap.title,
                    description=snap.description,
                    created_date=snap.created_at,
                    created_by=snap.created_by,
                    pv_count=counts.get(snap.id, 0),
                    meta_pvs=await self._get_meta_pv_values_for_snapshot(snap.id, meta_pvs),
                )
            )
        return result

    async def snapshot_exists(self, snapshot_id: str) -> bool:
        """Lightweight existence check (no value load) — used before enqueueing a restore."""
        return await self.snapshot_repo.get_by_id(snapshot_id) is not None

    async def get_by_id(self, snapshot_id: str, limit: int | None = None, offset: int = 0) -> SnapshotDTO | None:
        """Get a snapshot with its values (values optionally paginated)."""
        result = await self.snapshot_repo.get_with_values(snapshot_id, limit=limit, offset=offset)
        if not result:
            return None
        snapshot, values = result

        total_count = await self.value_repo.count_by_snapshot(snapshot_id)

        pv_ids = [v.pv_id for v in values]
        pvs_with_tags = await self.pv_repo.get_by_ids(pv_ids)
        pv_tag_map: dict[str, list[TagInfoDTO]] = {}
        pv_address_map: dict[str, tuple[str | None, str | None]] = {}  # pv_id -> (setpoint, readback)
        pv_device_map: dict[str, str | None] = {}
        for pv in pvs_with_tags:
            pv_tag_map[pv.id] = [TagInfoDTO(id=tag.id, name=tag.name, group_name=tag.group.name) for tag in pv.tags]
            pv_address_map[pv.id] = (pv.setpoint_address, pv.readback_address)
            pv_device_map[pv.id] = pv.device

        pv_values = []
        for v in values:
            setpoint_data = _parse_jsonb(v.setpoint_value)
            readback_data = _parse_jsonb(v.readback_value)
            setpoint_addr, readback_addr = pv_address_map.get(v.pv_id, (None, None))

            pv_values.append(
                PVValueDTO(
                    pv_id=v.pv_id,
                    pv_name=v.pv_name,
                    device=pv_device_map.get(v.pv_id),
                    setpoint_name=setpoint_addr,
                    readback_name=readback_addr,
                    setpoint_value=EpicsValueDTO(**setpoint_data) if setpoint_data else None,
                    readback_value=EpicsValueDTO(**readback_data) if readback_data else None,
                    status=v.status,
                    severity=v.severity,
                    timestamp=v.timestamp,
                    tags=pv_tag_map.get(v.pv_id, []),
                )
            )

        meta_pvs = [pv.pv_id for pv in await self.meta_pv_repo.get(active=True)]
        return SnapshotDTO(
            id=snapshot.id,
            title=snapshot.title,
            description=snapshot.description,
            created_date=snapshot.created_at,
            created_by=snapshot.created_by,
            pv_count=total_count,
            pv_values=pv_values,
            meta_pvs=await self._get_meta_pv_values_for_snapshot(snapshot.id, meta_pvs),
        )

    async def update_snapshot_metadata(
        self,
        snapshot_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> SnapshotSummaryDTO | None:
        """Update a snapshot's title and/or description."""
        snapshot = await self.snapshot_repo.update_metadata(snapshot_id, title=title, description=description)
        if not snapshot:
            return None

        pv_count = await self.snapshot_repo.get_value_count(snapshot.id)
        meta_pvs = [pv.pv_id for pv in await self.meta_pv_repo.get(active=True)]
        return SnapshotSummaryDTO(
            id=snapshot.id,
            title=snapshot.title,
            description=snapshot.description,
            created_date=snapshot.created_at,
            created_by=snapshot.created_by,
            pv_count=pv_count,
            meta_pvs=await self._get_meta_pv_values_for_snapshot(snapshot.id, meta_pvs),
        )

    async def create_snapshot(
        self, data: NewSnapshotDTO, created_by: str | None = None, progress_callback: Callable | None = None
    ) -> SnapshotSummaryDTO:
        """Create a snapshot by reading every registered PV from EPICS (the hot read path)."""
        logger.info("Creating snapshot: %s", data.title)
        start_time = datetime.now(UTC)

        pv_addresses = await self.pv_repo.get_all_addresses()
        total_pv_count = len(pv_addresses)

        # Collect the unique addresses to read (config addresses are not snapshotted).
        # An address can belong to more than one PV (only setpoint_address is unique;
        # readback addresses may be shared), so map each address to a *list* of pv_ids —
        # keying by a single pv_id would silently drop the reading for every other PV on
        # that address.
        setpoint_map: dict[str, list[str]] = {}  # address -> pv_ids
        readback_map: dict[str, list[str]] = {}  # address -> pv_ids
        for pv_id, setpoint, readback, _config in pv_addresses:
            if setpoint:
                setpoint_map.setdefault(setpoint, []).append(pv_id)
            if readback:
                readback_map.setdefault(readback, []).append(pv_id)

        all_addresses = list(set(setpoint_map.keys()) | set(readback_map.keys()))
        logger.info("Reading %d unique EPICS addresses for %d PVs", len(all_addresses), total_pv_count)

        # Rescale address-level progress to PV-level so the user sees "N/total PVs".
        async def pv_progress_callback(current_addr: int, total_addr: int, _message: str) -> None:
            pv_progress_count = int((current_addr / total_addr) * total_pv_count) if total_addr > 0 else 0
            await progress_callback(
                pv_progress_count, total_pv_count, f"Read {pv_progress_count:,}/{total_pv_count:,} PVs"
            )

        if progress_callback:
            epics_values = await self.epics.get_many_with_progress(all_addresses, pv_progress_callback)
        else:
            epics_values = await self.epics.get_many(all_addresses)

        logger.info("EPICS read completed in %.2fs", (datetime.now(UTC) - start_time).total_seconds())

        if progress_callback:
            await progress_callback(total_pv_count, total_pv_count, "Processing results...")

        # Create the snapshot row. The flush puts it in the open transaction so the
        # COPY below (same connection) sees it for the FK — no commit needed here.
        snapshot = Snapshot(title=data.title, description=data.description, created_by=created_by)
        snapshot = await self.snapshot_repo.create(snapshot)

        # Group the readings by PV, keeping only connected reads.
        pv_data: dict[str, dict] = {}  # pv_id -> {setpoint, readback, address}

        def _value_blob(epics_val: Any) -> dict:
            return {
                "value": _sanitize_for_json(epics_val.value),
                "status": epics_val.status,
                "severity": epics_val.severity,
                "timestamp": epics_val.timestamp.isoformat() if epics_val.timestamp else None,
                "units": epics_val.units,
                "precision": epics_val.precision,
            }

        for address, pv_ids in setpoint_map.items():
            epics_val = epics_values.get(address)
            for pv_id in pv_ids:
                pv_data.setdefault(pv_id, {"setpoint": None, "readback": None, "address": address})
                if epics_val and epics_val.connected:
                    pv_data[pv_id]["setpoint"] = _value_blob(epics_val)

        for address, pv_ids in readback_map.items():
            epics_val = epics_values.get(address)
            for pv_id in pv_ids:
                pv_data.setdefault(pv_id, {"setpoint": None, "readback": None, "address": address})
                if epics_val and epics_val.connected:
                    pv_data[pv_id]["readback"] = _value_blob(epics_val)

        now = datetime.now(UTC)
        snapshot_values_data = []
        for pv_id, data_dict in pv_data.items():
            setpoint = data_dict.get("setpoint")
            snapshot_values_data.append(
                {
                    "snapshot_id": snapshot.id,
                    "pv_id": pv_id,
                    "pv_name": data_dict.get("address", ""),
                    "setpoint_value": setpoint,
                    "readback_value": data_dict.get("readback"),
                    "status": setpoint.get("status") if setpoint else None,
                    "severity": setpoint.get("severity") if setpoint else None,
                    "timestamp": now,
                }
            )

        if progress_callback:
            await progress_callback(
                total_pv_count, total_pv_count, f"Saving {len(snapshot_values_data):,} PV values to database..."
            )

        count = await bulk_insert_snapshot_values(
            self.session, snapshot_values_data, progress_callback=progress_callback
        )

        logger.info("Snapshot created in %.2fs (%d values)", (datetime.now(UTC) - start_time).total_seconds(), count)

        meta_pvs = [pv.pv_id for pv in await self.meta_pv_repo.get(active=True)]
        return SnapshotSummaryDTO(
            id=snapshot.id,
            title=snapshot.title,
            description=snapshot.description,
            created_date=snapshot.created_at,
            created_by=snapshot.created_by,
            pv_count=count,
            meta_pvs=await self._get_meta_pv_values_for_snapshot(snapshot.id, meta_pvs),
        )

    async def restore_snapshot(
        self,
        snapshot_id: str,
        request: RestoreRequestDTO | None = None,
        progress_callback: Callable | None = None,
    ) -> RestoreResultDTO:
        """Restore a snapshot's setpoints to EPICS (the hot write path).

        Setpoints only, never readbacks. Every hook derives its extra writes up front,
        before anything is written; a hook error drops that PV from the write set so the
        system is never left half-restored. The setpoints go out as one batch, then each
        hook's extras as a batch of its own, in configured order, restricted to the PVs
        whose setpoint and earlier hook writes all succeeded. A failed extra write marks
        its primary address failed, with ``phase`` naming that hook, and later hooks skip
        the PV.

        Parameters
        ----------
        snapshot_id : str
            The snapshot to restore.
        request : RestoreRequestDTO | None
            Restores only ``request.pv_ids`` when given, otherwise the whole snapshot.
        progress_callback : Callable | None
            Async sink called as ``(current, total, message)``, where ``total`` is every
            EPICS write the restore will attempt: the setpoints plus all derived writes.
            Derived writes skipped because their setpoint or an earlier hook failed count
            as done, so the count always reaches ``total``.

        Returns
        -------
        RestoreResultDTO
            Totals plus a ``{pvId, pvName, error, phase}`` row per failed PV, where
            ``phase`` is ``"Setpoints"`` or the class name of the hook that failed
            (``MagnetTrimHook`` for the ``magnet_trim`` provider, and so on).
        """
        logger.info("Restoring snapshot: %s", snapshot_id)
        start_time = datetime.now(UTC)

        if request and request.pv_ids:
            values = await self.value_repo.get_by_snapshot_and_pvs(snapshot_id, request.pv_ids)
        else:
            values = await self.value_repo.get_by_snapshot(snapshot_id)

        if not values:
            return RestoreResultDTO(total_pvs=0, success_count=0, failure_count=0, failures=[])

        pv_ids = [v.pv_id for v in values]
        pvs = await self.pv_repo.get_by_ids(pv_ids)
        pv_map = {pv.id: pv for pv in pvs}

        # Build the write set — only setpoints, never readbacks.
        values_to_write: dict[str, Any] = {}
        pv_id_by_address: dict[str, str] = {}
        for val in values:
            pv = pv_map.get(val.pv_id)
            if not pv:
                continue
            if pv.setpoint_address and val.setpoint_value:
                setpoint_data = _parse_jsonb(val.setpoint_value)
                write_value = setpoint_data.get("value") if setpoint_data else None
                if write_value is not None:
                    values_to_write[pv.setpoint_address] = write_value
                    pv_id_by_address[pv.setpoint_address] = pv.id

        total_pvs = len(values_to_write)
        logger.info("Writing %d PV values", total_pvs)

        derived_per_hook: list[dict[str, list[tuple[str, Any]]]] = []
        hook_errors: dict[str, str] = {}
        for hook in self.restore_hooks:
            derived: dict[str, list[tuple[str, Any]]] = {}
            for addr, value in values_to_write.items():
                if addr in hook_errors:
                    continue
                try:
                    extras = hook.derive_extra_writes(addr, value)
                except RestoreHookError as e:
                    hook_errors[addr] = str(e)
                    continue
                if extras:
                    derived[addr] = extras
            derived_per_hook.append(derived)

        original_values = {addr: value for addr, value in values_to_write.items() if addr not in hook_errors}

        total_writes = len(original_values) + sum(
            len(extras) for derived in derived_per_hook for extras in derived.values()
        )

        if progress_callback:
            await progress_callback(0, total_writes, f"Starting restore of {total_pvs:,} PVs")

        results: dict[str, tuple[bool, str | None]] = {}
        failure_phase: dict[str, str] = {}
        if original_values:
            if progress_callback:
                results.update(
                    await self.epics.put_many_with_progress(
                        original_values, _sub_batch_progress(progress_callback, 0, total_writes)
                    )
                )
            else:
                results.update(await self.epics.put_many(original_values))
        writes_done = len(original_values)

        for address, (success, _error) in results.items():
            if not success:
                failure_phase[address] = PRIMARY_PHASE

        for hook, derived in zip(self.restore_hooks, derived_per_hook, strict=True):
            phase = type(hook).__name__

            extra_values: dict[str, Any] = {}
            primary_by_extra: dict[str, str] = {}
            derived_writes = 0
            for primary_addr, extras in derived.items():
                derived_writes += len(extras)
                if not results[primary_addr][0]:
                    continue
                for extra_addr, extra_val in extras:
                    extra_values[extra_addr] = extra_val
                    primary_by_extra[extra_addr] = primary_addr

            writes_done += derived_writes - len(extra_values)
            if not extra_values:
                continue

            if progress_callback:
                await progress_callback(writes_done, total_writes, f"Writing {len(extra_values):,} {phase} values...")
                hook_results = await self.epics.put_many_with_progress(
                    extra_values, _sub_batch_progress(progress_callback, writes_done, total_writes)
                )
            else:
                hook_results = await self.epics.put_many(extra_values)
            writes_done += len(extra_values)

            for extra_addr, (ok, err) in hook_results.items():
                if ok:
                    continue
                primary_addr = primary_by_extra[extra_addr]
                results[primary_addr] = (
                    False,
                    f"Primary write succeeded but {phase} write failed for {extra_addr}: {err or 'unknown error'}",
                )
                failure_phase[primary_addr] = phase

        for addr, err in hook_errors.items():
            results[addr] = (False, err)
            failure_phase[addr] = PRIMARY_PHASE

        failures = []
        success_count = 0
        for address, (success, error) in results.items():
            if success:
                success_count += 1
            else:
                pv_id = pv_id_by_address.get(address, "")
                failures.append(
                    {
                        "pvId": pv_id,
                        "pvName": address,
                        "error": error or "Unknown error",
                        "phase": failure_phase.get(address, PRIMARY_PHASE),
                    }
                )

        if progress_callback:
            await progress_callback(total_writes, total_writes, f"{success_count:,}/{total_pvs:,} PVs restored")
        logger.info(
            "Restore completed in %.2fs (%d success, %d failures)",
            (datetime.now(UTC) - start_time).total_seconds(),
            success_count,
            len(failures),
        )

        return RestoreResultDTO(
            total_pvs=total_pvs,
            success_count=success_count,
            failure_count=len(failures),
            failures=failures,
        )

    async def compare_snapshots(self, snapshot1_id: str, snapshot2_id: str) -> ComparisonResultDTO:
        """Compare two stored snapshots (setpoint + readback) under each PV's tolerances.

        Loads every value row for *both* snapshots into memory (no pagination) and runs on
        the request path, so comparing two very large (~100k-PV) snapshots is a sizeable
        synchronous allocation. Fine at current scale; a known v1 limit to revisit
        (stream/paginate the diff) if snapshot sizes grow.
        """
        result1 = await self.snapshot_repo.get_with_values(snapshot1_id)
        result2 = await self.snapshot_repo.get_with_values(snapshot2_id)
        if not result1 or not result2:
            raise SnapshotNotFound("One or both snapshots not found")
        _snap1, values1_list = result1
        _snap2, values2_list = result2

        values1 = {v.pv_id: v for v in values1_list}
        values2 = {v.pv_id: v for v in values2_list}

        all_pv_ids = set(values1.keys()) | set(values2.keys())
        pvs = await self.pv_repo.get_by_ids(list(all_pv_ids))
        pv_map = {pv.id: pv for pv in pvs}

        differences = []
        match_count = 0
        for pv_id in all_pv_ids:
            val1 = values1.get(pv_id)
            val2 = values2.get(pv_id)
            pv = pv_map.get(pv_id)

            setpoint1 = _parse_jsonb(val1.setpoint_value) if val1 else None
            setpoint2 = _parse_jsonb(val2.setpoint_value) if val2 else None
            v1 = setpoint1.get("value") if setpoint1 else None
            v2 = setpoint2.get("value") if setpoint2 else None
            readback1 = _parse_jsonb(val1.readback_value) if val1 else None
            readback2 = _parse_jsonb(val2.readback_value) if val2 else None
            r1 = readback1.get("value") if readback1 else None
            r2 = readback2.get("value") if readback2 else None

            abs_tol = pv.abs_tolerance if pv else 0
            rel_tol = pv.rel_tolerance if pv else 0

            has_setpoint = v1 is not None or v2 is not None
            has_readback = r1 is not None or r2 is not None

            setpoint_within = self._values_within_tolerance(v1, v2, abs_tol, rel_tol) if has_setpoint else True
            readback_within = self._values_within_tolerance(r1, r2, abs_tol, rel_tol) if has_readback else True

            if setpoint_within and readback_within:
                match_count += 1
            else:
                # Report the pair that is actually out of tolerance (prefer setpoint when
                # both differ) rather than always emitting the setpoint values — otherwise
                # a readback-only difference reports two equal setpoints.
                if not setpoint_within:
                    out_v1, out_v2 = v1, v2
                else:
                    out_v1, out_v2 = r1, r2
                differences.append(
                    {
                        "pvId": pv_id,
                        "pvName": val1.pv_name if val1 else (val2.pv_name if val2 else ""),
                        "value1": out_v1,
                        "value2": out_v2,
                        "withinTolerance": False,
                    }
                )

        return ComparisonResultDTO(
            snapshot1_id=snapshot1_id,
            snapshot2_id=snapshot2_id,
            differences=differences,
            match_count=match_count,
            difference_count=len(differences),
        )

    def _values_within_tolerance(self, v1: Any, v2: Any, abs_tol: float, rel_tol: float) -> bool:
        """Absolute tolerance first, then relative (vs. ``|v1|``); non-numeric → exact match."""
        if v1 is None and v2 is None:
            return True
        if v1 is None or v2 is None:
            return False
        try:
            f1, f2 = float(v1), float(v2)
            diff = abs(f1 - f2)
            if diff <= abs_tol:
                return True
            if rel_tol > 0 and f1 != 0:
                if diff / abs(f1) <= rel_tol:
                    return True
            return False
        except (TypeError, ValueError):
            return v1 == v2

    async def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot and all its values (DB cascade)."""
        return await self.snapshot_repo.delete_with_values(snapshot_id)
