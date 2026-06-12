"""In-memory deterministic adapter for tests.

The fake stores three flat sets (PVs, devices, elements) and pretends every
non-empty `pattern` is matched against them as a substring. That is enough to
exercise the cache, single-flight, and validate paths without needing real
regex/wildcard semantics; tests build their own pattern fixtures.
"""

import time
import asyncio

from canopy.raccoon.config import DirectoryInstanceConfig
from canopy.raccoon.adapters.base import (
    DEFAULT_PATTERN_METACHARS,
    InvalidQuery,
    AdapterHealth,
    DirectoryUnavailable,
    register_directory_type,
)


@register_directory_type("fake")
class FakeDirectoryAdapter:
    """Predictable adapter for tests.

    Test code mutates `.pvs`, `.devices`, `.elements`, `.delay_s`, and
    `.error` to drive the service under test.
    """

    name: str = "fake"
    type_name: str = "fake"
    pattern_metachars = DEFAULT_PATTERN_METACHARS

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.pvs: set[str] = set()
        self.devices: set[str] = set()
        self.elements: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.delay_s: float = 0.0
        self.error: Exception | None = None
        self.closed: bool = False

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "FakeDirectoryAdapter":
        return cls()

    async def _maybe_delay_and_fail(self) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error

    def _filter(self, pool: set[str], pattern: str) -> list[str]:
        if pattern == "%" or pattern == "":
            return sorted(pool)
        if pattern.startswith("(") and pattern.endswith(")"):
            options = pattern[1:-1].split("|")
            return sorted(p for p in pool if p in options)
        return sorted(p for p in pool if pattern in p or p == pattern)

    async def list_pvs(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        self.calls.append(("list_pvs", pattern))
        await self._maybe_delay_and_fail()
        return self._filter(self.pvs, pattern)

    async def list_devices(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        self.calls.append(("list_devices", pattern))
        await self._maybe_delay_and_fail()
        return self._filter(self.devices, pattern)

    async def list_elements(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        self.calls.append(("list_elements", pattern))
        await self._maybe_delay_and_fail()
        return self._filter(self.elements, pattern)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            healthy=not self.closed and self.error is None,
            latency_ms=0.1,
            last_error=str(self.error) if self.error else None,
            last_check_ts_ns=time.time_ns(),
        )

    async def close(self) -> None:
        self.closed = True


# Re-export the canopy errors so tests can build them ergonomically.
__all__ = [
    "FakeDirectoryAdapter",
    "InvalidQuery",
    "DirectoryUnavailable",
]
