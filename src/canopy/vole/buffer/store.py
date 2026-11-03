from __future__ import annotations

import asyncio

from canopy.vole.config import VoleConfig
from canopy.vole.buffer.ring import PerPVRing
from canopy.epics.pv_protocol import strip_protocol_prefix


class PVStore:
    """Registry of per-PV rings with lifecycle management, plus the pin registry.

    A pin names a holder (a live table eval) that needs the PV's ring and ingest to
    stay alive: pinned rings are skipped by LRU eviction and by grace teardown.
    Pins are keyed by the stripped PV name, the one grace and ingest use.
    """

    def __init__(self, config: VoleConfig) -> None:
        self._config = config
        self._rings: dict[str, PerPVRing] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pins: dict[str, set[str]] = {}  # stripped pv -> holders
        self._holders: dict[str, set[str]] = {}  # holder -> stripped pvs
        # Outages opened on a PV that has no ring yet (it disconnected before its first
        # sample); the ring takes the interval over when it is created.
        self._pending_outages: dict[str, int] = {}
        # Bumped whenever eviction could gain something to relieve: a ring is
        # created, a ring's ceiling grows, or a PV's last pin is released.
        self.generation: int = 0

    def get(self, pv: str) -> PerPVRing | None:
        return self._rings.get(pv)

    def get_or_create(
        self,
        pv: str,
        *,
        max_points: int | None = None,
    ) -> PerPVRing:
        if pv not in self._rings:
            self._rings[pv] = PerPVRing(
                max_points=max_points or self._config.max_points,
            )
            self._locks[pv] = asyncio.Lock()
            self.generation += 1
            since = self._pending_outages.pop(pv, None)
            if since is not None:
                self._rings[pv].open_outage(since)
        return self._rings[pv]

    def ensure_capacity(self, pv: str, max_points: int) -> PerPVRing:
        """Grow ``pv``'s ring to at least ``max_points``; a missing ring is created at ``config.max_points``."""
        ring = self.get_or_create(pv)
        if max_points > ring.max_points:
            ring.ensure_capacity(max_points)
            self.generation += 1
        return ring

    def drop(self, pv: str) -> bool:
        self._pending_outages.pop(pv, None)
        if pv in self._rings:
            del self._rings[pv]
            del self._locks[pv]
            return True
        return False

    def contains(self, pv: str) -> bool:
        return pv in self._rings

    @property
    def pv_names(self) -> list[str]:
        return list(self._rings.keys())

    @property
    def total_bytes(self) -> int:
        return sum(r.byte_estimate for r in self._rings.values())

    @property
    def total_points(self) -> int:
        return sum(r.count for r in self._rings.values())

    @property
    def ring_count(self) -> int:
        return len(self._rings)

    def lock_for(self, pv: str) -> asyncio.Lock:
        if pv not in self._locks:
            self._locks[pv] = asyncio.Lock()
        return self._locks[pv]

    # --- pins ------------------------------------------------------------- #

    def pin(self, pvs: list[str], holder: str) -> None:
        held = self._holders.setdefault(holder, set())
        for pv in pvs:
            name = strip_protocol_prefix(pv)
            held.add(name)
            self._pins.setdefault(name, set()).add(holder)

    def unpin(self, holder: str) -> list[str]:
        """Remove every pin of ``holder``; returns the PVs no longer pinned by anyone."""
        released: list[str] = []
        for name in sorted(self._holders.pop(holder, set())):
            holders = self._pins.get(name)
            if holders is None:
                continue
            holders.discard(holder)
            if not holders:
                del self._pins[name]
                released.append(name)
        if released:
            self.generation += 1
        return released

    def is_pinned(self, pv: str) -> bool:
        return strip_protocol_prefix(pv) in self._pins

    # --- outages ---------------------------------------------------------- #

    def open_outage(self, pv: str, now_ns: int) -> None:
        """Open an outage on ``pv``'s ring, or hold it until the ring is created."""
        ring = self._rings.get(pv)
        if ring is not None:
            ring.open_outage(now_ns)
        else:
            self._pending_outages.setdefault(pv, now_ns)

    def in_open_outage(self, pv: str) -> bool:
        """True while ``pv`` has an open outage, on its ring or still pending (no ring yet)."""
        ring = self._rings.get(pv)
        if ring is not None:
            return ring.outage_open
        return pv in self._pending_outages

    def close_outage(self, pv: str, now_ns: int) -> None:
        """Close ``pv``'s open outage.

        An outage opened before the PV had a ring creates the ring here (with the
        interval) since the reconnect sample that closes it is about to land in that
        ring anyway. A ring dropped (grace, eviction) mid-outage loses the interval
        with the rest of its state.
        """
        ring = self._rings.get(pv)
        if ring is None and pv in self._pending_outages:
            ring = self.get_or_create(pv)
        if ring is not None:
            ring.close_outage(now_ns)

    # --- eviction candidates ---------------------------------------------- #

    def lru_candidates(self) -> list[tuple[str, PerPVRing]]:
        """Unpinned rings sorted by last_access_ns ascending (LRU first)."""
        candidates = [(pv, ring) for pv, ring in self._rings.items() if pv not in self._pins]
        candidates.sort(key=lambda pair: pair[1].last_access_ns)
        return candidates

    def pinned_candidates(self) -> list[tuple[str, PerPVRing]]:
        """Pinned rings, largest first."""
        candidates = [(pv, ring) for pv, ring in self._rings.items() if pv in self._pins]
        candidates.sort(key=lambda pair: pair[1].count, reverse=True)
        return candidates
