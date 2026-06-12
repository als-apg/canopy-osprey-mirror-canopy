"""Minimal example `DirectoryAdapter` for the dotted-path loading test.

This is what an out-of-tree lab would write for Raccoon. It is *not*
registered via the `@register_directory_type` decorator; it is loaded via the
`module:Class` config form, demonstrating the no-registration path.
"""

import time

from canopy.raccoon.config import DirectoryInstanceConfig
from canopy.raccoon.adapters.base import AdapterHealth


class ExampleDirectoryAdapter:
    """Static example: returns a fixed canned name list per kind."""

    name: str = "example"
    type_name: str = "example"

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "ExampleDirectoryAdapter":
        return cls()

    async def list_pvs(self, pattern, *, tag=None, sort_by=None, element_type=None) -> list[str]:
        return ["PV:EXAMPLE"]

    async def list_devices(self, pattern, *, tag=None, sort_by=None, element_type=None) -> list[str]:
        return ["DEV:EXAMPLE"]

    async def list_elements(self, pattern, *, tag=None, sort_by=None, element_type=None) -> list[str]:
        return ["EL:EXAMPLE"]

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            healthy=True,
            latency_ms=0.0,
            last_error=None,
            last_check_ts_ns=time.time_ns(),
        )

    async def close(self) -> None:
        return
