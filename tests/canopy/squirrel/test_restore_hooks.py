"""Unit tests for the restore-hook registry + magnet-trim hook (no DB / EPICS)."""

import pytest

from canopy.squirrel.config import SquirrelConfig
from canopy.squirrel.restore_hooks import registry
from canopy.squirrel.restore_hooks.base import RestoreHookError
from canopy.squirrel.restore_hooks.magnet_trim import MAGNET_PREFIXES, MagnetTrimHook

hook = MagnetTrimHook()


def test_bdes_derives_ctrl_trim():
    assert hook.derive_extra_writes("QUAD:LI21:201:BDES") == [("QUAD:LI21:201:CTRL", "TRIM")]


@pytest.mark.parametrize("prefix", sorted(MAGNET_PREFIXES))
def test_every_magnet_prefix(prefix):
    assert hook.derive_extra_writes(f"{prefix}:DMP1:400:BDES") == [(f"{prefix}:DMP1:400:CTRL", "TRIM")]


def test_non_magnet_pv_yields_no_extra_writes():
    assert hook.derive_extra_writes("KLYS:LI22:31:KVAC") == []


def test_magnet_without_trailing_bdes_raises():
    with pytest.raises(RestoreHookError):
        hook.derive_extra_writes("QUAD:LI21:201:BCON")


def test_protocol_prefix_is_preserved():
    assert hook.derive_extra_writes("pva://QUAD:LI21:201:BDES") == [("pva://QUAD:LI21:201:CTRL", "TRIM")]


def test_bare_address_stays_bare():
    assert hook.derive_extra_writes("BEND:DMP1:400:BDES") == [("BEND:DMP1:400:CTRL", "TRIM")]


# --- registry --------------------------------------------------------------- #


def _reset_registry(monkeypatch, restore_hook: str):
    monkeypatch.setattr(registry, "_hook", None)
    monkeypatch.setattr(registry, "_hook_provider", None)
    monkeypatch.setattr(registry, "get_squirrel_config", lambda: SquirrelConfig(restore_hook=restore_hook))


def test_registry_none_when_unset(monkeypatch):
    _reset_registry(monkeypatch, "")
    assert registry.get_restore_hook() is None


def test_registry_builds_magnet_trim(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim")
    assert isinstance(registry.get_restore_hook(), MagnetTrimHook)


def test_registry_unknown_provider_returns_none(monkeypatch):
    _reset_registry(monkeypatch, "does_not_exist")
    assert registry.get_restore_hook() is None
