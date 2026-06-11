"""Adapter type registration: short-name lookup and the unknown-name path."""

from canopy.raccoon.adapters.base import get_registered_type, register_directory_type


def test_register_directory_type_stores_class() -> None:
    @register_directory_type("test_register_basic")
    class Adapter:
        pass

    assert get_registered_type("test_register_basic") is Adapter
    assert Adapter.type_name == "test_register_basic"


def test_get_registered_type_unknown_returns_none() -> None:
    assert get_registered_type("definitely-not-registered") is None


def test_register_decorator_returns_class() -> None:
    @register_directory_type("test_register_returns")
    class Adapter:
        marker = object()

    assert Adapter.marker is Adapter.marker
    assert get_registered_type("test_register_returns").marker is Adapter.marker
