"""Shared boot-time/uptime tracking for Canopy sub-modules.

Every sub-module needs to answer "how long have I been running" for its
``/health/watchdog`` heartbeat, without importing the FastAPI app (which would
create an import cycle back through the API layer). Each module keeps its own
tiny ``runtime.py`` that wraps a ``ModuleRuntime`` instance and re-exports
``mark_started``/``uptime_s`` as module-level functions, so existing import
paths (``from canopy.<module>.runtime import mark_started, uptime_s``) and
lifespan/health call sites are unaffected by this shared implementation.
"""

import time


class ModuleRuntime:
    """Tracks a single module's boot time so health endpoints can report uptime."""

    def __init__(self) -> None:
        self._start_monotonic: float = time.monotonic()

    def mark_started(self) -> None:
        """Record the boot time. Called from the parent app's lifespan startup."""
        self._start_monotonic = time.monotonic()

    def uptime_s(self) -> float:
        return time.monotonic() - self._start_monotonic
