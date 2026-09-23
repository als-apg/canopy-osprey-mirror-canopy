"""Unit tests for the restore-hook registry and the shipped hooks (no DB / EPICS)."""

import logging

import pytest

from canopy.squirrel.config import SquirrelConfig
from canopy.squirrel.restore_hooks import registry
from canopy.squirrel.restore_hooks.base import RestoreHookError
from canopy.squirrel.restore_hooks.magnet_trim import MAGNET_PREFIXES, MagnetTrimHook
from canopy.squirrel.restore_hooks.write_to_con import WriteToConHook

trim_hook = MagnetTrimHook()
con_hook = WriteToConHook()


# --- magnet trim ------------------------------------------------------------- #


def test_bdes_derives_ctrl_trim():
    assert trim_hook.derive_extra_writes("QUAD:LI21:201:BDES", 1.5) == [("QUAD:LI21:201:CTRL", "TRIM")]


@pytest.mark.parametrize("prefix", sorted(MAGNET_PREFIXES))
def test_every_magnet_prefix(prefix):
    assert trim_hook.derive_extra_writes(f"{prefix}:DMP1:400:BDES", 2.0) == [(f"{prefix}:DMP1:400:CTRL", "TRIM")]


def test_non_magnet_pv_yields_no_extra_writes():
    assert trim_hook.derive_extra_writes("KLYS:LI22:31:KVAC", 3.0) == []


def test_magnet_without_trailing_bdes_raises():
    with pytest.raises(RestoreHookError):
        trim_hook.derive_extra_writes("QUAD:LI21:201:BCON", 3.0)


def test_protocol_prefix_is_preserved():
    assert trim_hook.derive_extra_writes("pva://QUAD:LI21:201:BDES", 3.0) == [("pva://QUAD:LI21:201:CTRL", "TRIM")]


def test_bare_address_stays_bare():
    assert trim_hook.derive_extra_writes("BEND:DMP1:400:BDES", 3.0) == [("BEND:DMP1:400:CTRL", "TRIM")]


def test_only_trailing_bdes_is_swapped():
    assert trim_hook.derive_extra_writes("QUAD:BDES:120:BDES", 3.0) == [("QUAD:BDES:120:CTRL", "TRIM")]


# --- write to con ------------------------------------------------------------ #


def test_bdes_derives_bcon_carrying_the_value():
    assert con_hook.derive_extra_writes("QUAD:LI21:201:BDES", 4.25) == [("QUAD:LI21:201:BCON", 4.25)]


@pytest.mark.parametrize("prefix", sorted(MAGNET_PREFIXES))
def test_every_magnet_prefix_writes_to_con(prefix):
    assert con_hook.derive_extra_writes(f"{prefix}:DMP1:400:BDES", -1.5) == [(f"{prefix}:DMP1:400:BCON", -1.5)]


def test_con_non_magnet_pv_yields_no_extra_writes():
    assert con_hook.derive_extra_writes("KLYS:LI22:31:KVAC", 1.0) == []


def test_con_magnet_without_trailing_bdes_raises():
    with pytest.raises(RestoreHookError, match="not a BDES setpoint"):
        con_hook.derive_extra_writes("QUAD:LI21:201:CTRL", 1.0)


def test_con_error_names_the_original_address():
    with pytest.raises(RestoreHookError, match="pva://QUAD:LI21:201:RBV"):
        con_hook.derive_extra_writes("pva://QUAD:LI21:201:RBV", 1.0)


def test_con_protocol_prefix_is_preserved():
    assert con_hook.derive_extra_writes("pva://QUAD:LI21:201:BDES", 0.5) == [("pva://QUAD:LI21:201:BCON", 0.5)]


def test_con_bare_address_stays_bare():
    assert con_hook.derive_extra_writes("BEND:DMP1:400:BDES", 0.0) == [("BEND:DMP1:400:BCON", 0.0)]


# --- registry --------------------------------------------------------------- #


def _reset_registry(monkeypatch, restore_hooks: str, legacy_restore_hook: str = ""):
    monkeypatch.setattr(registry, "_hooks", [])
    monkeypatch.setattr(registry, "_hook_providers", None)
    monkeypatch.setattr(
        registry,
        "get_squirrel_config",
        lambda: SquirrelConfig(restore_hooks=restore_hooks, restore_hook=legacy_restore_hook),
    )


def test_registry_empty_when_unset(monkeypatch):
    _reset_registry(monkeypatch, "")
    assert registry.get_restore_hooks() == []


def test_registry_builds_magnet_trim(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim")
    hooks = registry.get_restore_hooks()
    assert len(hooks) == 1
    assert isinstance(hooks[0], MagnetTrimHook)


def test_registry_builds_both_hooks_in_configured_order(monkeypatch):
    _reset_registry(monkeypatch, "write_to_con,magnet_trim")
    hooks = registry.get_restore_hooks()
    assert [type(hook) for hook in hooks] == [WriteToConHook, MagnetTrimHook]


def test_registry_skips_unknown_name_and_warns(monkeypatch, caplog):
    _reset_registry(monkeypatch, " magnet_trim , nope ")
    with caplog.at_level(logging.WARNING, logger=registry.__name__):
        hooks = registry.get_restore_hooks()
    assert [type(hook) for hook in hooks] == [MagnetTrimHook]
    assert "nope" in caplog.text


def test_registry_dedupes_repeated_names(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim,magnet_trim")
    assert len(registry.get_restore_hooks()) == 1


def test_registry_caches_on_the_provider_list(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim")
    assert registry.get_restore_hooks() is registry.get_restore_hooks()


def test_registry_warns_that_the_legacy_variable_is_ignored(monkeypatch, caplog):
    _reset_registry(monkeypatch, "", legacy_restore_hook="magnet_trim")
    with caplog.at_level(logging.WARNING, logger=registry.__name__):
        hooks = registry.get_restore_hooks()
    assert hooks == []
    assert registry.LEGACY_SETTING_WARNING in caplog.text


def test_registry_is_quiet_without_the_legacy_variable(monkeypatch, caplog):
    _reset_registry(monkeypatch, "magnet_trim")
    with caplog.at_level(logging.WARNING, logger=registry.__name__):
        registry.get_restore_hooks()
    assert caplog.text == ""


def test_legacy_variable_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CANOPY_SQUIRREL__RESTORE_HOOK", "magnet_trim")
    assert SquirrelConfig().restore_hook == "magnet_trim"


# --- describe --------------------------------------------------------------- #


def test_describe_disabled_when_unset(monkeypatch):
    _reset_registry(monkeypatch, "")
    assert registry.describe_restore_hooks() == {
        "status": "disabled",
        "configured": [],
        "loaded": [],
        "unknown": [],
        "warnings": [],
        "available": ["magnet_trim", "write_to_con"],
    }


def test_describe_reports_the_legacy_variable(monkeypatch):
    _reset_registry(monkeypatch, "", legacy_restore_hook="magnet_trim")
    described = registry.describe_restore_hooks()
    assert described["status"] == "disabled"
    assert described["warnings"] == [registry.LEGACY_SETTING_WARNING]


def test_describe_active_when_all_known(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim,write_to_con")
    described = registry.describe_restore_hooks()
    assert described["status"] == "active"
    assert described["loaded"] == ["magnet_trim", "write_to_con"]
    assert described["unknown"] == []


def test_describe_partial_when_some_known(monkeypatch):
    _reset_registry(monkeypatch, "magnet_trim,nope")
    described = registry.describe_restore_hooks()
    assert described["status"] == "partial"
    assert described["loaded"] == ["magnet_trim"]
    assert described["unknown"] == ["nope"]


def test_describe_unknown_when_none_known(monkeypatch):
    _reset_registry(monkeypatch, "nope,also_nope")
    described = registry.describe_restore_hooks()
    assert described["status"] == "unknown"
    assert described["loaded"] == []
    assert described["unknown"] == ["nope", "also_nope"]


def test_describe_builds_no_hook(monkeypatch):
    """Diagnostics must never populate the registry cache."""
    _reset_registry(monkeypatch, "magnet_trim")
    registry.describe_restore_hooks()
    assert registry._hooks == []
    assert registry._hook_providers is None
