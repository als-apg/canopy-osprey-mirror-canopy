"""Live data ingestion from Hare's PVBroker into Vole ring buffers.

Vole registers as an internal "client" of the Hare broker using the same
add_client/subscribe API that WebSocket clients use. Diff frames arrive
at ~100ms intervals; each PVValue is converted to a Sample and appended
to the corresponding ring.

Every sample is stamped with the server's receive time and an ``after_connect``
flag (an ``initial`` frame, the first sample after a disconnected entry, or a
PV's first sample at all). Disconnected entries open an outage interval on the
ring, closed by the next connected sample -- both on the receive clock. The
broker sends this channel a ``flush`` marker after every flush, empty ones
included; ``watermark_ns`` is the receive time of the newest marker, i.e. the
newest flush ingest has fully applied.

This module is guarded by try/except at import sites — it degrades
gracefully when Hare is not available (tests, standalone Mole-only mode).
"""

from __future__ import annotations

import logging
from typing import Any

from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.vole.services.buffer_service import BufferService

logger = logging.getLogger(__name__)


class VoleChannel:
    """Implements Hare's ClientChannel protocol for in-process ingestion.

    The broker calls ``send_json`` with diff/initial/heartbeat/flush frames.
    Vole applies "diff" and "initial" frames and takes its watermark from "flush".
    """

    # Asks the broker for a ``flush`` marker after every flush, empty ones included.
    wants_flush_marker = True

    def __init__(self, ingest: HareIngestService) -> None:
        self._ingest = ingest

    async def send_json(self, data: Any) -> None:
        # Never let an ingestion error escape: the broker treats a send_json
        # exception as a dead websocket and would silently unregister Vole,
        # permanently stopping live ingestion for every PV. A malformed frame
        # must cost us at most that frame.
        try:
            if not isinstance(data, dict):
                return
            frame_type = data.get("type")
            if frame_type in ("diff", "initial"):
                pv_data = data.get("data")
                if isinstance(pv_data, dict):
                    await self._ingest._handle_frame(pv_data, frame_type)
            elif frame_type == "flush":
                self._ingest._on_flush()
        except Exception:
            logger.warning(
                "Failed to ingest %r frame",
                data.get("type") if isinstance(data, dict) else type(data).__name__,
                exc_info=True,
            )

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
        # Canonical name -> the subscribed names that feed its ring (``X`` and ``ca://X`` alike).
        self._by_canonical: dict[str, set[str]] = {}
        self._running = False
        self._watermark_ns = 0
        self._seen: set[str] = set()  # PVs that have stored at least one sample
        self._disconnected: set[str] = set()  # PVs whose last entry was disconnected

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
            self._by_canonical.clear()
        await self._broker.remove_client(self.CLIENT_ID)
        logger.info("HareIngestService stopped")

    async def subscribe(self, pvs: list[str], *, wait_seed: bool = True) -> list[str]:
        """Subscribe Vole to live updates for these PVs via Hare.

        Returns which of ``pvs`` are subscribed afterwards. Only names the broker
        accepted are recorded — a rejected one would make GraceManager believe
        there is a buffer to tear down. ``wait_seed=False`` returns before the
        broker's seed reads land, so a dead name does not cost the CA timeout.
        """
        new_pvs = [pv for pv in pvs if pv not in self._subscribed_pvs]
        if new_pvs:
            if wait_seed:
                accepted = await self._broker.subscribe(self.CLIENT_ID, new_pvs)
            else:
                accepted = await self._broker.subscribe(self.CLIENT_ID, new_pvs, wait_seed=False)
            self._subscribed_pvs.update(accepted)
            for pv in accepted:
                self._by_canonical.setdefault(strip_protocol_prefix(pv), set()).add(pv)
            logger.debug("Vole subscribed to %d/%d new PVs: %s", len(accepted), len(new_pvs), accepted[:5])
        return [pv for pv in pvs if pv in self._subscribed_pvs]

    async def unsubscribe(self, pvs: list[str]) -> None:
        """Unsubscribe Vole from live updates for these PVs."""
        to_unsub = [pv for pv in pvs if pv in self._subscribed_pvs]
        if not to_unsub:
            return
        await self._broker.unsubscribe(self.CLIENT_ID, to_unsub)
        self._subscribed_pvs -= set(to_unsub)
        for pv in to_unsub:
            name = strip_protocol_prefix(pv)
            aliases = self._by_canonical.get(name)
            if aliases is not None:
                aliases.discard(pv)
                if not aliases:
                    del self._by_canonical[name]
            self._seen.discard(name)
            self._disconnected.discard(name)
        logger.debug("Vole unsubscribed from %d PVs", len(to_unsub))

    async def unsubscribe_canonical(self, names: list[str]) -> None:
        """Unsubscribe every subscribed name whose canonical form is one of ``names``.

        A ring is keyed by the canonical name while subscriptions keep the name
        as bound, so dropping ring ``X`` must release ``ca://X`` and ``X`` alike;
        otherwise the name stays subscribed and a later subscribe gets no seed.
        """
        to_unsub = [pv for name in names for pv in self._by_canonical.get(strip_protocol_prefix(name), ())]
        if to_unsub:
            await self.unsubscribe(to_unsub)

    def is_subscribed(self, name: str) -> bool:
        """True while some subscribed name feeds ``name``'s ring (compared on canonical names)."""
        return strip_protocol_prefix(name) in self._by_canonical

    @property
    def subscribed_pvs(self) -> set[str]:
        return set(self._subscribed_pvs)

    @property
    def watermark_ns(self) -> int:
        """Receive time of the newest Hare flush ingest has fully applied (0 before the first)."""
        return self._watermark_ns

    def _on_flush(self) -> None:
        self._watermark_ns = self._buffer.clock.now_ns()

    async def _handle_frame(self, data: dict[str, dict], frame_type: str = "diff") -> None:
        """Process a diff/initial frame: convert each PVValue to a Sample and append.

        The whole per-PV body is guarded so one malformed PV entry (e.g. a
        non-numeric ``tsNs``) skips only that PV — the rest of the frame still
        lands, and no exception propagates back to the broker.
        """
        recv_ns = self._buffer.clock.now_ns()
        for pv_name, pv_dict in data.items():
            if not isinstance(pv_dict, dict):
                continue
            try:
                canonical_pv = strip_protocol_prefix(pv_name)
                if not pv_dict.get("connected", True):
                    if canonical_pv not in self._disconnected:
                        self._disconnected.add(canonical_pv)
                        self._buffer.open_outage(canonical_pv, recv_ns)
                    continue

                sample = self._pv_value_to_sample(pv_dict)
                if sample is None:
                    continue
                after_connect = (
                    frame_type == "initial" or canonical_pv in self._disconnected or canonical_pv not in self._seen
                )
                if canonical_pv in self._disconnected:
                    self._disconnected.discard(canonical_pv)
                    self._buffer.close_outage(canonical_pv, recv_ns)
                await self._buffer.append(canonical_pv, sample, recv_ns=recv_ns, after_connect=after_connect)
                self._seen.add(canonical_pv)
            except Exception:
                logger.warning("Failed to ingest live sample for %s", pv_name, exc_info=True)

    @staticmethod
    def _pv_value_to_sample(pv_dict: dict) -> Sample | None:
        """Convert a PVValue wire dict to a Sample for ring storage."""
        ts_ns = pv_dict.get("tsNs") or pv_dict.get("ts_ns")
        value = pv_dict.get("value")
        severity_raw = pv_dict.get("severity", "NO_ALARM")

        if ts_ns is None:
            return None
        # A bool is an int in Python but never a valid timestamp; anything
        # non-numeric (string, dict, ...) would blow up in int() below.
        if isinstance(ts_ns, bool) or not isinstance(ts_ns, int | float):
            logger.debug("Skipping sample for %s: non-numeric tsNs %r", pv_dict.get("pv"), ts_ns)
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
