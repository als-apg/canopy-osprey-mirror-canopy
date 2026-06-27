"""Snapshot domain service: create / restore / compare + listing."""

from __future__ import annotations

import json
import math
import logging
from typing import Any
from datetime import UTC, datetime
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from canopy.squirrel.errors import SnapshotNotFound
from canopy.squirrel.schemas.snapshot import (
    PVValueDTO,
    SnapshotDTO,
    TagInfoDTO,
    EpicsValueDTO,
    MetaPVValueDTO,
    NewSnapshotDTO,
    RestoreResultDTO,
    RestoreRequestDTO,
    SnapshotSummaryDTO,
    ComparisonResultDTO,
)
from canopy.squirrel.data.models import Snapshot
from canopy.squirrel.restore_hooks import RestoreHook, RestoreHookError, get_restore_hook
from canopy.squirrel.adapters.epics_io import EpicsIO
from canopy.squirrel.data.repositories import (
    PvRepository,
    MetaPvRepository,
    SnapshotRepository,
    SnapshotValueRepository,
)
from canopy.squirrel.services.bulk_insert import bulk_insert_snapshot_values

logger = logging.getLogger(__name__)


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
    """Convert ``NaN``/``Inf``/``-Inf`` (invalid JSON) to ``None``, recursing into lists/dicts."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    return value


class SnapshotService:
    """Snapshot operations with EPICS integration. Owns no transaction — the caller
    (request dep or worker ``session_scope``) commits."""

    def __init__(
        self,
        session: AsyncSession,
        epics_io: EpicsIO | None = None,
        restore_hook: RestoreHook | None = None,
    ):
        # ``epics_io`` is required only for create/restore (run in the worker, which
        # builds the connector). The read endpoints construct the service without it.
        self.session = session
        self.snapshot_repo = SnapshotRepository(session)
        self.value_repo = SnapshotValueRepository(session)
        self.pv_repo = PvRepository(session)
        self.meta_pv_repo = MetaPvRepository(session)
        self.epics = epics_io
        # Fall back to the configured singleton when not explicitly injected.
        # Tests can pass ``restore_hook=...`` to control behavior in isolation.
        self.restore_hook = restore_hook or get_restore_hook()

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
        self, title: str | None = None, tag_ids: list[str] | None = None
    ) -> list[SnapshotSummaryDTO]:
        """List snapshots, optionally filtered by title and/or tags."""
        snapshots = await self.snapshot_repo.search(title=title, tag_ids=tag_ids)

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
        snapshot = await self.snapshot_repo.get_with_values(snapshot_id, limit=limit, offset=offset)
        if not snapshot:
            return None

        total_count = await self.value_repo.count_by_snapshot(snapshot_id)

        pv_ids = [v.pv_id for v in snapshot.values]
        pvs_with_tags = await self.pv_repo.get_by_ids(pv_ids)
        pv_tag_map: dict[str, list[TagInfoDTO]] = {}
        pv_address_map: dict[str, tuple[str | None, str | None]] = {}  # pv_id -> (setpoint, readback)
        pv_device_map: dict[str, str | None] = {}
        for pv in pvs_with_tags:
            pv_tag_map[pv.id] = [TagInfoDTO(id=tag.id, name=tag.name, group_name=tag.group.name) for tag in pv.tags]
            pv_address_map[pv.id] = (pv.setpoint_address, pv.readback_address)
            pv_device_map[pv.id] = pv.device

        pv_values = []
        for v in snapshot.values:
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

        return SnapshotDTO(
            id=snapshot.id,
            title=snapshot.title,
            description=snapshot.description,
            created_date=snapshot.created_at,
            created_by=snapshot.created_by,
            pv_count=total_count,
            pv_values=pv_values,
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
        setpoint_map: dict[str, str] = {}  # address -> pv_id
        readback_map: dict[str, str] = {}  # address -> pv_id
        for pv_id, setpoint, readback, _config in pv_addresses:
            if setpoint:
                setpoint_map[setpoint] = pv_id
            if readback:
                readback_map[readback] = pv_id

        all_addresses = list(set(setpoint_map.keys()) | set(readback_map.keys()))
        logger.info("Reading %d unique EPICS addresses for %d PVs", len(all_addresses), total_pv_count)

        # Rescale address-level progress to PV-level so the user sees "N/total PVs".
        async def pv_progress_callback(current_addr: int, total_addr: int, _message: str) -> None:
            pv_progress_count = int((current_addr / total_addr) * total_pv_count) if total_addr > 0 else 0
            await progress_callback(pv_progress_count, total_pv_count, f"Read {pv_progress_count:,}/{total_pv_count:,} PVs")

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

        for address, pv_id in setpoint_map.items():
            pv_data.setdefault(pv_id, {"setpoint": None, "readback": None, "address": address})
            epics_val = epics_values.get(address)
            if epics_val and epics_val.connected:
                pv_data[pv_id]["setpoint"] = _value_blob(epics_val)

        for address, pv_id in readback_map.items():
            pv_data.setdefault(pv_id, {"setpoint": None, "readback": None, "address": address})
            epics_val = epics_values.get(address)
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

        logger.info(
            "Snapshot created in %.2fs (%d values)", (datetime.now(UTC) - start_time).total_seconds(), count
        )

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

        Writes setpoints only; restore-hook extras are derived before the write.
        A hook error skips that PV's primary  write and surfaces as a failure
        Magnets are trimmed only if their primary write succeeded.
        A trim failure is attributed to the primary address; progress
        is measured in EPICS writes (primary + trim).
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

        if progress_callback:
            await progress_callback(0, total_pvs, f"Starting restore of {total_pvs:,} PVs")

        # Derive any extra writes (e.g. magnet trims) up front, before any write.
        magnet_trim_pv_names: dict[str, list[tuple[str, Any]]] = {}
        hook_errors: dict[str, str] = {}
        if self.restore_hook is not None:
            for addr in values_to_write:
                try:
                    extras = self.restore_hook.derive_extra_writes(addr)
                except RestoreHookError as e:
                    hook_errors[addr] = str(e)
                    continue
                if extras:
                    magnet_trim_pv_names[addr] = extras

        # A hook error drops its PV from the write set so the system isn't left
        # inconsistent; the error is surfaced as a failure below.
        original_values = {addr: value for addr, value in values_to_write.items() if addr not in hook_errors}

        # Progress denominator = every primary setpoint plus every derived write.
        trim_writes_total = sum(len(extras) for extras in magnet_trim_pv_names.values())
        total_writes = len(original_values) + trim_writes_total

        results: dict[str, tuple[bool, str | None]] = {}
        if original_values:
            if progress_callback:

                async def _primary_progress(current: int, _subtotal: int, message: str) -> None:
                    await progress_callback(current, total_writes, message)

                results.update(await self.epics.put_many_with_progress(original_values, _primary_progress))
            else:
                results.update(await self.epics.put_many(original_values))

        # Only trim magnets whose setpoint write succeeded.
        magnet_trim_values: dict[str, Any] = {}
        base_magnet_pv_name: dict[str, str] = {}
        for primary_addr, extras in magnet_trim_pv_names.items():
            primary_ok, _ = results.get(primary_addr, (False, None))
            if not primary_ok:
                continue
            for extra_addr, extra_val in extras:
                magnet_trim_values[extra_addr] = extra_val
                base_magnet_pv_name[extra_addr] = primary_addr

        # Writes accounted for: all setpoints + the trims we skip (their setpoint failed).
        writes_done = len(original_values) + (trim_writes_total - len(magnet_trim_values))

        if magnet_trim_values:
            trim_base = writes_done

            async def _trim_progress(current: int, _subtotal: int, message: str) -> None:
                await progress_callback(trim_base + current, total_writes, message)

            if progress_callback:
                await progress_callback(
                    trim_base, total_writes, f"Writing {len(magnet_trim_values):,} magnet trim values..."
                )
            trim_results = await self.epics.put_many_with_progress(
                magnet_trim_values, _trim_progress if progress_callback else None
            )
            for extra_addr, (ok, err) in trim_results.items():
                if not ok:
                    primary_addr = base_magnet_pv_name[extra_addr]
                    results[primary_addr] = (
                        False,
                        f"Primary write succeeded but trim write failed for {extra_addr}: {err or 'unknown error'}",
                    )

        # Hook validation errors (malformed magnet addresses) become per-PV failures.
        for addr, err in hook_errors.items():
            results[addr] = (False, err)

        failures = []
        success_count = 0
        for address, (success, error) in results.items():
            if success:
                success_count += 1
            else:
                pv_id = pv_id_by_address.get(address, "")
                failures.append({"pvId": pv_id, "pvName": address, "error": error or "Unknown error"})

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
        """Compare two stored snapshots (setpoint + readback) under each PV's tolerances."""
        snap1 = await self.snapshot_repo.get_with_values(snapshot1_id)
        snap2 = await self.snapshot_repo.get_with_values(snapshot2_id)
        if not snap1 or not snap2:
            raise SnapshotNotFound("One or both snapshots not found")

        values1 = {v.pv_id: v for v in snap1.values}
        values2 = {v.pv_id: v for v in snap2.values}

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
                out_v1 = v1 if has_setpoint else r1
                out_v2 = v2 if has_setpoint else r2
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
