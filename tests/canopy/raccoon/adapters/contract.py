"""Reusable adapter conformance assertions.

Functions here exercise the `DirectoryAdapter` protocol's behavioral contract.
Pass any adapter; the assertions match `Module-Architecture-Raccoon.md` §12.
Out-of-tree adapter authors can call these from their own test suite to verify
their implementation conforms.
"""

from canopy.raccoon.adapters.base import AdapterHealth, DirectoryAdapter


async def assert_identity_attributes(adapter: DirectoryAdapter) -> None:
    assert isinstance(adapter.name, str) and adapter.name
    assert isinstance(adapter.type_name, str) and adapter.type_name


async def assert_health(adapter: DirectoryAdapter) -> None:
    h = await adapter.health()
    assert isinstance(h, AdapterHealth)
    assert isinstance(h.healthy, bool)
    assert isinstance(h.last_check_ts_ns, int)


async def assert_close_idempotent(adapter: DirectoryAdapter) -> None:
    await adapter.close()
    await adapter.close()


def _assert_name_list(names: object) -> None:
    assert isinstance(names, list), "List calls must return a list"
    for n in names:
        assert isinstance(n, str) and n, "List calls must return non-empty string names"


async def assert_list_pvs_invariants(adapter: DirectoryAdapter, pattern: str) -> None:
    _assert_name_list(await adapter.list_pvs(pattern, tag=None, sort_by=None, element_type=None))


async def assert_list_devices_invariants(adapter: DirectoryAdapter, pattern: str) -> None:
    _assert_name_list(await adapter.list_devices(pattern, tag=None, sort_by=None, element_type=None))


async def assert_list_elements_invariants(adapter: DirectoryAdapter, pattern: str) -> None:
    _assert_name_list(await adapter.list_elements(pattern, tag=None, sort_by=None, element_type=None))
