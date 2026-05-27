"""AdapterRegistry behavior."""

import pytest

from canopy.mole.config import MoleConfig, ArchiverInstanceConfig
from canopy.mole.adapters.base import ArchiverNotConfigured
from canopy.mole.adapters.registry import (
    AdapterRegistry,
    build_registry,
    _resolve_adapter_class,
)

# Importing the fakes module side-effect-registers the "fake" type via decorator.
from tests.canopy.mole.adapters._fakes import FakeArchiverAdapter


def test_get_default_when_no_name_given():
    fake = FakeArchiverAdapter(name="primary")
    registry = AdapterRegistry({"primary": fake}, default="primary")
    assert registry.get(None) is fake
    assert registry.get("primary") is fake


def test_get_unknown_name_raises():
    fake = FakeArchiverAdapter(name="primary")
    registry = AdapterRegistry({"primary": fake}, default="primary")
    with pytest.raises(ArchiverNotConfigured) as exc_info:
        registry.get("missing")
    assert "missing" in exc_info.value.detail


def test_no_default_no_name_raises():
    registry = AdapterRegistry({}, default="")
    with pytest.raises(ArchiverNotConfigured):
        registry.get(None)


def test_default_must_be_in_adapters():
    fake = FakeArchiverAdapter(name="a")
    with pytest.raises(ArchiverNotConfigured) as exc_info:
        AdapterRegistry({"a": fake}, default="missing")
    assert "missing" in exc_info.value.detail


def test_resolve_short_name():
    cls = _resolve_adapter_class("fake")
    assert cls is FakeArchiverAdapter


def test_resolve_unknown_short_name_raises():
    with pytest.raises(ArchiverNotConfigured):
        _resolve_adapter_class("does_not_exist")


def test_resolve_dotted_path():
    cls = _resolve_adapter_class("tests.canopy.mole.adapters._fakes:FakeArchiverAdapter")
    assert cls is FakeArchiverAdapter


def test_resolve_dotted_path_bad_module():
    with pytest.raises(ArchiverNotConfigured):
        _resolve_adapter_class("nonexistent.module.path:Klass")


def test_resolve_dotted_path_bad_class():
    with pytest.raises(ArchiverNotConfigured):
        _resolve_adapter_class("tests.canopy.mole.adapters._fakes:NoSuchClass")


def test_build_registry_from_config():
    config = MoleConfig(
        default_archiver="primary",
        archivers={"primary": ArchiverInstanceConfig(type="fake", url="http://nope")},
    )
    registry = build_registry(config)
    assert registry.default_name == "primary"
    assert "primary" in registry.names()
    assert isinstance(registry.get("primary"), FakeArchiverAdapter)


def test_build_registry_with_dotted_path():
    config = MoleConfig(
        default_archiver="ext",
        archivers={
            "ext": ArchiverInstanceConfig(
                type="tests.canopy.mole.adapters._fakes:FakeArchiverAdapter",
                url="http://nope",
            )
        },
    )
    registry = build_registry(config)
    assert isinstance(registry.get("ext"), FakeArchiverAdapter)


async def test_list_with_health_concurrent():
    a = FakeArchiverAdapter(name="a")
    b = FakeArchiverAdapter(name="b")
    registry = AdapterRegistry({"a": a, "b": b}, default="a")
    health = await registry.list_with_health(timeout_s=1.0)
    assert set(health.keys()) == {"a", "b"}
    assert all(h.healthy for h in health.values())
