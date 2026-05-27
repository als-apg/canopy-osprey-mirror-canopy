"""SLAC built-in adapter: wraps the synchronous `meme.names` Python client.

`meme` is the SLAC EPICS v4 Directory Service Python wrapper. It exposes
synchronous list functions (`list_pvs`, `list_devices`, `list_elements`).
Each call is dispatched to a thread via `asyncio.to_thread` so the FastAPI
event loop stays responsive; the per-call timeout is enforced by the service
layer above us via `asyncio.wait_for`.

The `meme` import is lazy: this module imports cleanly without `meme`
installed, so tests can patch `meme.names` (or pass a stub directly into the
constructor) without needing the real package on disk.
"""

import time
import asyncio
import logging
from typing import Any
from importlib import import_module

from canopy.raccoon.config import DirectoryInstanceConfig
from canopy.raccoon.adapters.base import (
    AdapterHealth,
    InvalidQuery,
    DirectoryTimeout,
    DirectoryUnavailable,
    AdapterMisconfigured,
    register_directory_type,
)

logger = logging.getLogger(__name__)


@register_directory_type("meme")
class MemeDirectoryAdapter:
    """Built-in adapter for SLAC's EPICS v4 Directory Service via `meme.names`."""

    name: str = "meme"
    type_name: str = "meme"

    def __init__(self, names_module: Any | None = None) -> None:
        self._names = names_module

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "MemeDirectoryAdapter":
        try:
            names_module = import_module("meme.names")
        except ImportError as exc:
            raise AdapterMisconfigured(
                "The 'meme' package is not installed. Install Canopy's optional 'meme' "
                "dependency group, or configure a different DirectoryAdapter."
            ) from exc
        return cls(names_module=names_module)

    # ------------------------------------------------------------------ #
    # Internal call dispatch
    # ------------------------------------------------------------------ #

    async def _call(self, fn_name: str, pattern: str, **kwargs: Any) -> list[str]:
        if self._names is None:
            raise AdapterMisconfigured("MemeDirectoryAdapter has no `meme.names` module bound")
        fn = getattr(self._names, fn_name)
        try:
            return await asyncio.to_thread(fn, pattern, **kwargs)
        except TimeoutError as exc:
            raise DirectoryTimeout(f"meme.{fn_name} timed out: {exc}") from exc
        except (ConnectionError, OSError) as exc:
            raise DirectoryUnavailable(f"meme.{fn_name} could not reach the directory: {exc}") from exc
        except ValueError as exc:
            raise InvalidQuery(f"meme.{fn_name} rejected query: {exc}") from exc
        except (DirectoryUnavailable, DirectoryTimeout, InvalidQuery):
            raise
        except Exception as exc:
            logger.exception("Unexpected error from meme.%s", fn_name)
            raise DirectoryUnavailable(f"meme.{fn_name} failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Adapter Protocol
    # ------------------------------------------------------------------ #

    async def list_pvs(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        kwargs: dict[str, Any] = {}
        if tag is not None:
            kwargs["tag"] = tag
        if sort_by is not None:
            kwargs["sort"] = sort_by
        if element_type is not None:
            kwargs["element_type"] = element_type
        return await self._call("list_pvs", pattern, **kwargs)

    async def list_devices(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        kwargs: dict[str, Any] = {}
        if tag is not None:
            kwargs["tag"] = tag
        if sort_by is not None:
            kwargs["sort"] = sort_by
        if element_type is not None:
            kwargs["element_type"] = element_type
        return await self._call("list_devices", pattern, **kwargs)

    async def list_elements(
        self,
        pattern: str,
        *,
        tag: str | None = None,
        sort_by: str | None = None,
        element_type: str | None = None,
    ) -> list[str]:
        kwargs: dict[str, Any] = {}
        if tag is not None:
            kwargs["tag"] = tag
        if sort_by is not None:
            kwargs["sort"] = sort_by
        if element_type is not None:
            kwargs["element_type"] = element_type
        return await self._call("list_elements", pattern, **kwargs)

    async def health(self) -> AdapterHealth:
        """Probe by issuing a deliberately-empty list_pvs call.

        A round-trip that returns (even with zero results) means the upstream
        is reachable. Any exception is captured as `last_error` with
        `healthy=False`.
        """
        start = time.monotonic()
        try:
            await self.list_pvs("__healthcheck__:nonexistent")
            return AdapterHealth(
                healthy=True,
                latency_ms=(time.monotonic() - start) * 1000,
                last_error=None,
                last_check_ts_ns=time.time_ns(),
            )
        except Exception as exc:
            return AdapterHealth(
                healthy=False,
                latency_ms=(time.monotonic() - start) * 1000,
                last_error=str(exc),
                last_check_ts_ns=time.time_ns(),
            )

    async def close(self) -> None:
        """No-op: `meme.names` keeps no per-instance resources to release."""
        return
