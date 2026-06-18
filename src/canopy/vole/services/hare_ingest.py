"""Live data ingestion from Hare's PVBroker into Vole ring buffers.

Vole registers as an internal "client" of the Hare broker using the same
add_client/subscribe API that WebSocket clients use. Diff frames arrive
at ~100ms intervals; each PVValue is converted to a Sample and appended
to the corresponding ring.

This module is guarded by try/except at import sites — it degrades
gracefully when Hare is not available (tests, standalone Mole-only mode).
"""

from __future__ import annotations

import logging
from typing import Any

from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.vole.services.buffer_service import BufferService

logger = logging.getLogger(__name__)


def _strip_protocol_prefix(pv: str) -> str:
    """Remove ca:// or pva:// prefix if present."""
    if pv.startswith("ca://"):
        return pv[5:]
    if pv.startswith("pva://"):
        return pv[6:]
    return pv


class VoleChannel:
    """Implements Hare's ClientChannel protocol for in-process ingestion.

    The broker calls ``send_json`` with diff/initial/heartbeat frames.
    Vole only cares about "diff" and "initial" frames.
    """

    def __init__(self, ingest: HareIngestService) -> None:
        self._ingest = ingest

    async def send_json(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        frame_type = data.get("type")
        if frame_type in ("diff", "initial"):
            pv_data = data.get("data")
            if isinstance(pv_data, dict):
                await self._ingest._handle_frame(pv_data)

    async def close(self) -> None:
        pass


class HareIngestService:
    """Bridges Hare's PVBroker to Vole's BufferService for live data ingestion."""

    CLIENT_ID = "vole-internal"

    def __init__(self, buffer_service: BufferService, broker: Any) -> None:
        self._buffer = buffer_service
        self._broker = broker
        self._channel = VoleChannel(self)
        self._subscribed_pvs: set[str] = set()
        self._running = False

    async def start(self) -> None:
        self._broker.add_client(self.CLIENT_ID, self._channel)
        self._running = True
        logger.info("HareIngestService started (client_id=%s)", self.CLIENT_ID)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._subscribed_pvs:
            await self._broker.unsubscribe(self.CLIENT_ID, list(self._subscribed_pvs))
            self._subscribed_pvs.clear()
        await self._broker.remove_client(self.CLIENT_ID)
        logger.info("HareIngestService stopped")

    async def subscribe(self, pvs: list[str]) -> None:
        """Subscribe Vole to live updates for these PVs via Hare."""
        new_pvs = [pv for pv in pvs if pv not in self._subscribed_pvs]
        if not new_pvs:
            return
        await self._broker.subscribe(self.CLIENT_ID, new_pvs)
        self._subscribed_pvs.update(new_pvs)
        logger.debug("Vole subscribed to %d PVs: %s", len(new_pvs), new_pvs[:5])

    async def unsubscribe(self, pvs: list[str]) -> None:
        """Unsubscribe Vole from live updates for these PVs."""
        to_unsub = [pv for pv in pvs if pv in self._subscribed_pvs]
        if not to_unsub:
            return
        await self._broker.unsubscribe(self.CLIENT_ID, to_unsub)
        self._subscribed_pvs -= set(to_unsub)
        logger.debug("Vole unsubscribed from %d PVs", len(to_unsub))

    @property
    def subscribed_pvs(self) -> set[str]:
        return set(self._subscribed_pvs)

    async def _handle_frame(self, data: dict[str, dict]) -> None:
        """Process a diff/initial frame: convert each PVValue to a Sample and append."""
        for pv_name, pv_dict in data.items():
            if not isinstance(pv_dict, dict):
                continue
            if not pv_dict.get("connected", True):
                continue

            sample = self._pv_value_to_sample(pv_dict)
            if sample is None:
                continue

            canonical_pv = _strip_protocol_prefix(pv_name)
            try:
                await self._buffer.append(canonical_pv, sample)
            except Exception:
                logger.debug("Failed to append live sample for %s", canonical_pv, exc_info=True)

    @staticmethod
    def _pv_value_to_sample(pv_dict: dict) -> Sample | None:
        """Convert a PVValue wire dict to a Sample for ring storage."""
        ts_ns = pv_dict.get("tsNs") or pv_dict.get("ts_ns")
        value = pv_dict.get("value")
        severity_raw = pv_dict.get("severity", "NO_ALARM")

        if ts_ns is None:
            return None

        if isinstance(severity_raw, str):
            try:
                severity = AlarmSeverity(severity_raw)
            except ValueError:
                severity = AlarmSeverity.INVALID
        else:
            severity = AlarmSeverity.INVALID

        return Sample(
            ts_ns=int(ts_ns),
            value=value,
            severity=severity,
            source="live",
        )
