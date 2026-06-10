"""ArchiverService: selects adapter, dispatches, normalizes errors, decimates fallback.

Lifecycle: one instance per `canopy-api` process. Constructed in
`canopy.mole.app.mole_lifespan`; injected into `canopy.mole.gateway` via the
module-level singleton there. Tests can re-bind freely.
"""

import asyncio
import logging
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from canopy.mole.config import MoleConfig
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import Sample, BinnedPoint
from canopy.mole.adapters.base import ArchiverAdapter, LastBeforeNotSupported
from canopy.mole.schemas.archiver import (
    ArchiverInfoDTO,
    ArchiverListDTO,
    HealthSummaryDTO,
    ArchiverHealthDTO,
)
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.services.decimation import uniform_time_decimate

logger = logging.getLogger(__name__)


class ArchiverService:
    """Public business logic. Routes call this; gateway facades wrap this."""

    def __init__(self, registry: AdapterRegistry, config: MoleConfig) -> None:
        self._registry = registry
        self._config = config

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    def _resolve(self, name: str | None) -> ArchiverAdapter:
        return self._registry.get(name)

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        archiver: str | None = None,
        max_points: int | None = None,
        include_last_before: bool = False,
    ) -> tuple[str, list[Sample], bool]:
        """Raw read. Returns `(archiver_name, samples, decimated)`.

        `decimated` is True when the service-side fallback engaged (adapter
        returned more than `max_points`).

        `include_last_before=True` prepends the most recent sample strictly
        before `t0_ns` (at its true timestamp) so sparse on-change PVs have a
        defined value at the window's left edge. The prior sample is fetched
        concurrently with the window, is exempt from `max_points` (which keeps
        bounding the in-window samples), and is omitted when none is archived.
        """
        adapter = self._resolve(archiver)
        prior: Sample | None = None
        if include_last_before:
            fetch_last_before = getattr(adapter, "fetch_last_before", None)
            if fetch_last_before is None:
                raise LastBeforeNotSupported(f"Archiver '{adapter.name}' does not support includeLastBefore")
            # `t0_ns - 1`: a sample exactly at the window start is already in-window.
            samples, prior = await asyncio.gather(
                adapter.fetch_window(pv, t0_ns=t0_ns, t1_ns=t1_ns, max_points=max_points),
                fetch_last_before(pv, t0_ns - 1),
            )
        else:
            samples = await adapter.fetch_window(pv, t0_ns=t0_ns, t1_ns=t1_ns, max_points=max_points)

        decimated = False
        if max_points is not None and len(samples) > max_points:
            samples = uniform_time_decimate(samples, max_points)
            decimated = True

        if prior is not None and prior.ts_ns < t0_ns:
            samples = [prior, *samples]

        return adapter.name, samples, decimated

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning: Literal["auto", "time"],
        archiver: str | None = None,
        target_buckets: int | None = None,
        bin_size_ns: int | None = None,
    ) -> tuple[str, list[BinnedPoint]]:
        """Binned read. Returns `(archiver_name, points)`."""
        adapter = self._resolve(archiver)
        points = await adapter.fetch_binned(
            pv,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
            binning=binning,
            target_buckets=target_buckets,
            bin_size_ns=bin_size_ns,
        )
        return adapter.name, points

    async def metadata(self, pv: str, *, archiver: str | None = None) -> PVMetadata:
        adapter = self._resolve(archiver)
        return await adapter.metadata(pv)

    async def search(
        self,
        pattern: str,
        *,
        archiver: str | None = None,
        limit: int = 100,
    ) -> tuple[str, list[str]]:
        adapter = self._resolve(archiver)
        pvs = await adapter.search(pattern, limit=limit)
        return adapter.name, pvs

    async def list_archivers(self) -> ArchiverListDTO:
        """Return the configured archivers with current health.

        Concurrent fan-out via the registry; a single archiver being down does
        not fail the call (it surfaces in that archiver's health entry).
        """
        health_map = await self._registry.list_with_health()
        archivers: list[ArchiverInfoDTO] = []
        for name, adapter in self._registry.items():
            cfg = self._config.archivers.get(name)
            url = _redact_url(cfg.url) if cfg else ""
            health = health_map.get(name)
            archivers.append(
                ArchiverInfoDTO(
                    name=name,
                    type_name=getattr(adapter, "type_name", "unknown"),
                    url=url,
                    healthy=health.healthy if health else False,
                    latency_ms=health.latency_ms if health else None,
                    last_error=health.last_error if health else None,
                )
            )
        return ArchiverListDTO(default=self._registry.default_name, archivers=archivers)

    async def health_summary(self) -> HealthSummaryDTO:
        """Per-archiver rollup. See `HealthSummaryDTO.status` for semantics."""
        health_map = await self._registry.list_with_health()
        archivers = [
            ArchiverHealthDTO(
                name=name,
                healthy=h.healthy,
                latency_ms=h.latency_ms,
                last_error=h.last_error,
                last_check_ts_ns=h.last_check_ts_ns,
            )
            for name, h in health_map.items()
        ]
        if not archivers or all(a.healthy for a in archivers):
            status: Literal["ok", "degraded", "down"] = "ok"
        elif all(not a.healthy for a in archivers):
            status = "down"
        else:
            status = "degraded"
        return HealthSummaryDTO(status=status, archivers=archivers)


def _redact_url(url: str) -> str:
    """Strip embedded `user:password@` credentials from a URL."""
    parts = urlsplit(url)
    if "@" in parts.netloc:
        _, _, host = parts.netloc.rpartition("@")
        return urlunsplit(parts._replace(netloc=f"***@{host}"))
    return url
