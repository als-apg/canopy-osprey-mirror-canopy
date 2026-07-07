"""Restore-hook plugin registry.

``get_restore_hook`` returns the configured hook or ``None`` when no plugin is
selected (``CANOPY_SQUIRREL__RESTORE_HOOK`` unset/empty). The ``module:Class``
dotted-path escape hatch for out-of-tree lab hooks is a Phase-2 addition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from canopy.squirrel.config import SquirrelConfig, get_squirrel_config
from canopy.squirrel.restore_hooks.base import RestoreHook
from canopy.squirrel.restore_hooks.magnet_trim import MagnetTrimHook

logger = logging.getLogger(__name__)


def _build_magnet_trim(_config: SquirrelConfig) -> RestoreHook:
    return MagnetTrimHook()


RESTORE_HOOK_PROVIDERS: dict[str, Callable[[SquirrelConfig], RestoreHook]] = {
    "magnet_trim": _build_magnet_trim,
}


_hook: RestoreHook | None = None
_hook_provider: str | None = None


def get_restore_hook() -> RestoreHook | None:
    """Return the configured hook singleton, or ``None`` when no hook is selected."""
    global _hook, _hook_provider

    config = get_squirrel_config()
    provider = (config.restore_hook or "").strip()

    if not provider:
        return None

    if _hook is not None and _hook_provider == provider:
        return _hook

    factory = RESTORE_HOOK_PROVIDERS.get(provider)
    if factory is None:
        logger.warning("Unknown CANOPY_SQUIRREL__RESTORE_HOOK=%r; no restore hook will run.", provider)
        return None

    _hook = factory(config)
    _hook_provider = provider
    logger.info("Restore hook initialized: %s", provider)
    return _hook
