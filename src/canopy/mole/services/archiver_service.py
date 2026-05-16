"""ArchiverService — selects adapter, dispatches, normalizes errors, decimates fallback.

Lifecycle: one instance per `canopy-api` process. Constructed in
`canopy.mole.app.mole_lifespan`; injected into `canopy.mole.gateway` via the
module-level singleton there. Tests can re-bind freely.
"""

import logging

from canopy.contracts.sample import Sample
from canopy.mole.config import MoleConfig
from canopy.mole.adapters.base import ArchiverAdapter
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
    ) -> tuple[str, list[Sample], bool]:
        """Raw read. Returns `(archiver_name, samples, decimated)`.

        `decimated` is True when the service-side fallback engaged (adapter
        returned more than `max_points`).
        """
        adapter = self._resolve(archiver)
        samples = await adapter.fetch_window(pv, t0_ns=t0_ns, t1_ns=t1_ns, max_points=max_points)

        decimated = False
        if max_points is not None and len(samples) > max_points:
            samples = uniform_time_decimate(samples, max_points)
            decimated = True

        return adapter.name, samples, decimated
