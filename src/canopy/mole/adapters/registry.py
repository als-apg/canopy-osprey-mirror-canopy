"""Adapter instance registry.

Built once at app startup from `MoleConfig.archivers`. Each entry's `type` field
resolves to either a registered short name (`@register_archiver_type`-decorated
class) or an out-of-tree class via a `module:Class` dotted path.
"""

import time
import asyncio
import logging
import importlib
from collections.abc import Iterable

from canopy.mole.config import MoleConfig
from canopy.mole.adapters.base import (
    AdapterHealth,
    ArchiverAdapter,
    ArchiverNotConfigured,
    get_registered_type,
)

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """Holds adapter instances by name, with default-aware lookup."""

    def __init__(self, adapters: dict[str, ArchiverAdapter], default: str) -> None:
        if default and default not in adapters:
            raise ArchiverNotConfigured(f"default archiver '{default}' is not configured")
        self._adapters = adapters
        self._default = default

    def get(self, name: str | None) -> ArchiverAdapter:
        target = name or self._default
        if not target:
            raise ArchiverNotConfigured("No archiver specified and no default configured")
        try:
            return self._adapters[target]
        except KeyError:
            raise ArchiverNotConfigured(f"Archiver '{target}' is not configured") from None

    @property
    def default_name(self) -> str:
        return self._default

    def names(self) -> list[str]:
        return list(self._adapters.keys())

    def items(self) -> Iterable[tuple[str, ArchiverAdapter]]:
        return self._adapters.items()

    async def list_with_health(self, timeout_s: float = 2.0) -> dict[str, AdapterHealth]:
        """Probe every adapter concurrently. Per-probe failures don't fail the call."""

        async def _probe(name: str, adapter: ArchiverAdapter) -> tuple[str, AdapterHealth]:
            start = time.monotonic()
            try:
                h = await asyncio.wait_for(adapter.health(), timeout=timeout_s)
                return name, h
            except TimeoutError:
                return name, AdapterHealth(
                    healthy=False,
                    latency_ms=(time.monotonic() - start) * 1000,
                    last_error="health probe timed out",
                    last_check_ts_ns=time.time_ns(),
                )
            except Exception as exc:
                return name, AdapterHealth(
                    healthy=False,
                    latency_ms=(time.monotonic() - start) * 1000,
                    last_error=str(exc),
                    last_check_ts_ns=time.time_ns(),
                )

        results = await asyncio.gather(*(_probe(n, a) for n, a in self._adapters.items()))
        return dict(results)

    async def close_all(self) -> None:
        """Close every adapter; log but don't propagate errors (shutdown path)."""
        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception:
                logger.exception("Error closing adapter %s", name)


def _resolve_adapter_class(type_str: str) -> type[ArchiverAdapter]:
    """Resolve a config `type` string to an adapter class.

    Two forms supported:
      - ``"<short_name>"`` — previously ``@register_archiver_type``-decorated class.
      - ``"module.path:ClassName"`` — dynamic import for out-of-tree adapters.
    """
    if ":" in type_str:
        module_path, _, class_name = type_str.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ArchiverNotConfigured(f"Cannot import adapter module '{module_path}': {exc}") from exc
        cls = getattr(module, class_name, None)
        if cls is None:
            raise ArchiverNotConfigured(f"Adapter class '{class_name}' not found in '{module_path}'")
        return cls

    cls = get_registered_type(type_str)
    if cls is None:
        raise ArchiverNotConfigured(
            f"Unknown adapter type '{type_str}'. Use a registered short name or a 'module:Class' dotted path."
        )
    return cls


def build_registry(config: MoleConfig) -> AdapterRegistry:
    """Construct an AdapterRegistry from MoleConfig at app startup.

    Builds each adapter via its `from_config(name, cfg)` classmethod and stores
    it in the registry. Fails loudly on misconfiguration.
    """
    adapters: dict[str, ArchiverAdapter] = {}
    for name, cfg in config.archivers.items():
        cls = _resolve_adapter_class(cfg.type)
        adapter = cls.from_config(name, cfg)
        adapters[name] = adapter
    return AdapterRegistry(adapters, default=config.default_archiver)
