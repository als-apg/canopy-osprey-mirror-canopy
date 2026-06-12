"""Adapter resolution + a degenerate one-instance registry.

Raccoon has exactly one configured directory backend per replica. The registry
is a single slot, but the type-resolution mechanism mirrors Mole's so that the
adapter type stays pluggable: a `module:Class` dotted path in config loads any
out-of-tree adapter that has been pip-installed alongside Canopy.
"""

import logging
import importlib

from canopy.raccoon.config import RaccoonConfig
from canopy.raccoon.adapters.base import (
    DirectoryAdapter,
    AdapterMisconfigured,
    get_registered_type,
)

logger = logging.getLogger(__name__)


class DirectoryRegistry:
    """Holds the configured `DirectoryAdapter` (or none, if unconfigured)."""

    def __init__(self, adapter: DirectoryAdapter | None) -> None:
        self._adapter = adapter

    @property
    def adapter(self) -> DirectoryAdapter | None:
        return self._adapter

    def require(self) -> DirectoryAdapter:
        if self._adapter is None:
            raise AdapterMisconfigured("No directory adapter is configured")
        return self._adapter

    async def close(self) -> None:
        if self._adapter is None:
            return
        try:
            await self._adapter.close()
        except Exception:
            logger.exception("Error closing directory adapter %s", self._adapter.name)


def _resolve_adapter_class(type_str: str) -> type[DirectoryAdapter]:
    """Resolve a config `type` string to an adapter class.

    Two forms supported:
      - ``"<short_name>"`` -- previously ``@register_directory_type``-decorated class.
      - ``"module.path:ClassName"`` -- dynamic import for out-of-tree adapters.
    """
    if ":" in type_str:
        module_path, _, class_name = type_str.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise AdapterMisconfigured(f"Cannot import adapter module '{module_path}': {exc}") from exc
        cls = getattr(module, class_name, None)
        if cls is None:
            raise AdapterMisconfigured(f"Adapter class '{class_name}' not found in '{module_path}'")
        return cls

    cls = get_registered_type(type_str)
    if cls is None:
        raise AdapterMisconfigured(
            f"Unknown adapter type '{type_str}'. Use a registered short name or a 'module:Class' dotted path."
        )
    return cls


def build_registry(config: RaccoonConfig) -> DirectoryRegistry:
    """Construct a `DirectoryRegistry` from `RaccoonConfig` at app startup.

    A missing `directory` block yields an empty registry; routes that require
    the adapter raise `AdapterMisconfigured` (500). This keeps the API mounted
    in deployments where Raccoon is configured later via env reload.
    """
    if config.directory is None:
        return DirectoryRegistry(None)
    cls = _resolve_adapter_class(config.directory.type)
    adapter = cls.from_config(config.directory)
    if not getattr(adapter, "name", None):
        adapter.name = getattr(adapter, "type_name", config.directory.type)
    return DirectoryRegistry(adapter)
