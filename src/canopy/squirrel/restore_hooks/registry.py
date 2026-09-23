"""Restore-hook plugin registry.

``get_restore_hooks`` returns the hooks named by ``CANOPY_SQUIRREL__RESTORE_HOOKS``
(comma-separated provider keys, applied in the order given) — an empty list when
nothing is selected. The ``module:Class`` dotted-path escape hatch for out-of-tree
lab hooks is a Phase-2 addition.
"""

from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable

from canopy.squirrel.config import SquirrelConfig, get_squirrel_config
from canopy.squirrel.restore_hooks.base import RestoreHook
from canopy.squirrel.restore_hooks.magnet_trim import MagnetTrimHook
from canopy.squirrel.restore_hooks.write_to_con import WriteToConHook

logger = logging.getLogger(__name__)


def _build_magnet_trim(_config: SquirrelConfig) -> RestoreHook:
    """Build the magnet-trim hook."""
    return MagnetTrimHook()


def _build_write_to_con(_config: SquirrelConfig) -> RestoreHook:
    """Build the write-to-BCON hook."""
    return WriteToConHook()


RESTORE_HOOK_PROVIDERS: dict[str, Callable[[SquirrelConfig], RestoreHook]] = {
    "magnet_trim": _build_magnet_trim,
    "write_to_con": _build_write_to_con,
}


_hooks: list[RestoreHook] = []
_hook_providers: list[str] | None = None


def _configured_providers(config: SquirrelConfig) -> list[str]:
    """Return a config's provider keys, in order, without repeats.

    Parameters
    ----------
    config : SquirrelConfig
        The squirrel configuration to read ``restore_hooks`` from.

    Returns
    -------
    list[str]
        Provider keys parsed from ``CANOPY_SQUIRREL__RESTORE_HOOKS``, de-duplicated
        with first-seen order preserved.
    """
    return list(dict.fromkeys(config.restore_hooks_list))


LEGACY_SETTING_WARNING = (
    "CANOPY_SQUIRREL__RESTORE_HOOK (singular) is set but no longer read; "
    "list the providers in CANOPY_SQUIRREL__RESTORE_HOOKS instead"
)


def _config_warnings(config: SquirrelConfig) -> list[str]:
    """Return operator-facing problems with a config that would otherwise pass silently.

    Parameters
    ----------
    config : SquirrelConfig
        The squirrel configuration to inspect.

    Returns
    -------
    list[str]
        ``LEGACY_SETTING_WARNING`` when the retired singular variable is set, since
        it no longer selects anything; empty otherwise.
    """
    return [LEGACY_SETTING_WARNING] if config.restore_hook.strip() else []


def get_restore_hooks() -> list[RestoreHook]:
    """Return the configured hook singletons, built once per provider list.

    Returns
    -------
    list[RestoreHook]
        Hook instances in configured order. Unregistered provider keys are logged and
        skipped, so a typo disables only that hook. Empty when nothing is configured.
    """
    global _hooks, _hook_providers

    config = get_squirrel_config()
    providers = _configured_providers(config)

    if providers == _hook_providers:
        return _hooks

    hooks: list[RestoreHook] = []
    for provider in providers:
        factory = RESTORE_HOOK_PROVIDERS.get(provider)
        if factory is None:
            logger.warning("Unknown CANOPY_SQUIRREL__RESTORE_HOOKS entry %r; skipping it.", provider)
            continue
        hooks.append(factory(config))

    _hooks = hooks
    _hook_providers = providers
    for warning in _config_warnings(config):
        logger.warning(warning)
    logger.info("Restore hooks initialized: %s", [type(hook).__name__ for hook in hooks])
    return _hooks


def describe_restore_hooks() -> dict[str, Any]:
    """Report configured versus registered restore hooks, for diagnostics.

    Read-only: builds and caches nothing, so an operator can confirm that the
    ``CANOPY_SQUIRREL__RESTORE_HOOKS`` they set reached the process and resolved to
    known providers without triggering a restore.

    Returns
    -------
    dict[str, Any]
        ``status`` — ``"disabled"`` when nothing is configured, ``"active"`` when
        every configured key is registered, ``"unknown"`` when none is, ``"partial"``
        when some are. ``configured`` — the configured keys in order. ``loaded`` —
        those that will run. ``unknown`` — those that match no provider.
        ``warnings`` — configuration problems that change nothing but deserve
        attention, such as the retired singular variable still being set.
        ``available`` — every registered provider key, sorted.
    """
    config = get_squirrel_config()
    configured = _configured_providers(config)
    loaded = [name for name in configured if name in RESTORE_HOOK_PROVIDERS]
    unknown = [name for name in configured if name not in RESTORE_HOOK_PROVIDERS]

    if not configured:
        status = "disabled"
    elif not unknown:
        status = "active"
    elif not loaded:
        status = "unknown"
    else:
        status = "partial"

    return {
        "status": status,
        "configured": configured,
        "loaded": loaded,
        "unknown": unknown,
        "warnings": _config_warnings(config),
        "available": sorted(RESTORE_HOOK_PROVIDERS),
    }
