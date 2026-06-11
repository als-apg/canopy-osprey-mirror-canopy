"""SLAC built-in adapter: wraps the synchronous `meme.names` Python client.

`meme` is the SLAC EPICS v4 Directory Service Python wrapper. It is
distributed through SLAC's package source — the unrelated `meme` project on
PyPI is NOT it, so Canopy does not declare it as an installable extra; deploys
that use this adapter install SLAC's `meme` alongside Canopy themselves.

`meme.names` exposes synchronous list functions (`list_pvs`, `list_devices`,
`list_elements`). Each call is dispatched to an adapter-owned bounded thread
pool so the FastAPI event loop stays responsive; the per-call timeout is
enforced by the service layer above us via `asyncio.wait_for`. The pool is
deliberately separate from asyncio's default `to_thread` executor: a timed-out
call's thread keeps running until the upstream returns, so a hung directory
service can only saturate this adapter's own workers, not the process-wide
pool the rest of Canopy shares.

The `meme` import is lazy: this module imports cleanly without `meme`
installed, so tests can patch `meme.names` (or pass a stub directly into the
constructor) without needing the real package on disk.
"""

import time
import asyncio
import logging
import functools
from typing import Any
from importlib import import_module
from concurrent.futures import ThreadPoolExecutor

from canopy.raccoon.config import DirectoryInstanceConfig
from canopy.raccoon.adapters.base import (
    DEFAULT_PATTERN_METACHARS,
    InvalidQuery,
    AdapterHealth,
    DirectoryTimeout,
    AdapterMisconfigured,
    DirectoryUnavailable,
    register_directory_type,
)

logger = logging.getLogger(__name__)

# Upper bound on concurrent in-flight `meme.names` calls. Hung upstream calls
# pin their worker thread until the upstream returns, so this is also the
# blast-radius cap for a wedged directory service. Override via the config
# `extras` block: `CANOPY_RACCOON__DIRECTORY__EXTRAS__MAX_WORKERS=8`.
DEFAULT_MAX_WORKERS = 16


@register_directory_type("meme")
class MemeDirectoryAdapter:
    """Built-in adapter for SLAC's EPICS v4 Directory Service via `meme.names`."""

    name: str = "meme"
    type_name: str = "meme"
    pattern_metachars = DEFAULT_PATTERN_METACHARS

    def __init__(self, names_module: Any | None = None, *, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        self._names = names_module
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="meme-directory")

    @classmethod
    def from_config(cls, cfg: DirectoryInstanceConfig) -> "MemeDirectoryAdapter":
        try:
            names_module = import_module("meme.names")
        except ImportError as exc:
            raise AdapterMisconfigured(
                "The 'meme' package (SLAC's EPICS v4 Directory Service wrapper) is not "
                "installed. It is distributed through SLAC's package source — the 'meme' "
                "project on PyPI is an unrelated package. Install SLAC's wrapper alongside "
                "Canopy, or configure a different DirectoryAdapter via the 'module:Class' "
                "form of CANOPY_RACCOON__DIRECTORY__TYPE."
            ) from exc
        try:
            max_workers = int(cfg.extras.get("max_workers", DEFAULT_MAX_WORKERS))
        except (TypeError, ValueError) as exc:
            raise AdapterMisconfigured(f"extras.max_workers must be an integer: {exc}") from exc
        return cls(names_module=names_module, max_workers=max_workers)

    # ------------------------------------------------------------------ #
    # Internal call dispatch
    # ------------------------------------------------------------------ #

    async def _call(self, fn_name: str, pattern: str, **kwargs: Any) -> list[str]:
        if self._names is None:
            raise AdapterMisconfigured("MemeDirectoryAdapter has no `meme.names` module bound")
        fn = getattr(self._names, fn_name)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, functools.partial(fn, pattern, **kwargs))
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
            kwargs["sort_by"] = sort_by
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
            kwargs["sort_by"] = sort_by
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
            kwargs["sort_by"] = sort_by
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
        """Release the adapter-owned worker pool.

        Queued-but-unstarted calls are cancelled; threads stuck in a hung
        upstream call are abandoned rather than waited on, so shutdown does
        not block behind a wedged directory service.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
