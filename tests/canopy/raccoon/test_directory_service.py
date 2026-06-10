"""DirectoryService: cache hit/miss, opportunistic warm, exists, validate, single-flight."""

import asyncio

import pytest

from canopy.raccoon.cache.keys import NameKind
from canopy.raccoon.adapters.base import InvalidQuery, DirectoryTimeout, DirectoryUnavailable


@pytest.mark.asyncio
async def test_list_caches_result_by_pattern(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"BPMS:1", "BPMS:2", "XCOR:1"}

    first = await directory_service.list(NameKind.PV, "BPMS")
    second = await directory_service.list(NameKind.PV, "BPMS")

    assert first == ["BPMS:1", "BPMS:2"]
    assert second == ["BPMS:1", "BPMS:2"]
    assert sum(1 for c in fake_adapter.calls if c[0] == "list_pvs") == 1


@pytest.mark.asyncio
async def test_list_distinguishes_kinds(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"P:1"}
    fake_adapter.devices = {"D:1"}
    fake_adapter.elements = {"E:1"}

    assert await directory_service.list(NameKind.PV, "%") == ["P:1"]
    assert await directory_service.list(NameKind.DEVICE, "%") == ["D:1"]
    assert await directory_service.list(NameKind.ELEMENT, "%") == ["E:1"]


@pytest.mark.asyncio
async def test_list_pvs_warms_name_cache_with_positives(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"BPMS:1", "BPMS:2"}
    await directory_service.list(NameKind.PV, "BPMS")

    assert await directory_service.exists("BPMS:1") is True
    # The exists() above must have been served from the warmed name_cache,
    # not by issuing another upstream call.
    assert sum(1 for c in fake_adapter.calls if c[0] == "list_pvs") == 1


@pytest.mark.asyncio
async def test_exists_caches_negative_then_serves_from_cache(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = set()
    assert await directory_service.exists("MISSING:1") is False
    assert await directory_service.exists("MISSING:1") is False

    list_pv_calls = [c for c in fake_adapter.calls if c[0] == "list_pvs"]
    assert len(list_pv_calls) == 1


@pytest.mark.asyncio
async def test_exists_rejects_pattern_metacharacters(directory_service) -> None:
    with pytest.raises(InvalidQuery):
        await directory_service.exists("BPMS:%")


@pytest.mark.asyncio
async def test_validate_partitions_and_preserves_input_order(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"A:1", "C:1"}
    result = await directory_service.validate(["A:1", "B:1", "C:1", "B:1"])

    assert result.known == ["A:1", "C:1"]
    assert result.unknown == ["B:1", "B:1"]


@pytest.mark.asyncio
async def test_validate_rejects_pattern_metacharacters(directory_service) -> None:
    with pytest.raises(InvalidQuery):
        await directory_service.validate(["A:1", "B:%"])


@pytest.mark.asyncio
async def test_validate_uses_name_cache_to_skip_upstream(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"A:1"}
    # Warm the name_cache.
    await directory_service.exists("A:1")
    fake_adapter.calls.clear()

    result = await directory_service.validate(["A:1", "A:1"])

    assert result.known == ["A:1", "A:1"]
    assert result.unknown == []
    assert fake_adapter.calls == []


@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_identical_lookups(make_directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"X:1", "X:2"}
    fake_adapter.delay_s = 0.05
    service = make_directory_service(fake_adapter)

    results = await asyncio.gather(*(service.list(NameKind.PV, "X") for _ in range(20)))

    assert all(r == ["X:1", "X:2"] for r in results)
    assert sum(1 for c in fake_adapter.calls if c[0] == "list_pvs") == 1


@pytest.mark.asyncio
async def test_timeout_translates_to_directory_timeout(make_directory_service, fake_adapter) -> None:
    fake_adapter.delay_s = 0.5
    service = make_directory_service(fake_adapter, timeout_s=0.05)

    with pytest.raises(DirectoryTimeout):
        await service.list(NameKind.PV, "anything")


@pytest.mark.asyncio
async def test_adapter_error_propagates(make_directory_service, fake_adapter) -> None:
    fake_adapter.error = DirectoryUnavailable("boom")
    service = make_directory_service(fake_adapter)

    with pytest.raises(DirectoryUnavailable):
        await service.list(NameKind.PV, "%")


@pytest.mark.asyncio
async def test_stats_reports_adapter_and_caches(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"A:1"}
    await directory_service.list(NameKind.PV, "A")
    await directory_service.list(NameKind.PV, "A")

    stats = await directory_service.stats()

    assert stats.adapter_name == "fake"
    assert stats.adapter_type == "fake"
    assert stats.adapter_health.healthy is True
    assert stats.pattern_cache.size == 1
    assert stats.pattern_cache.hits >= 1


@pytest.mark.asyncio
async def test_exists_does_not_populate_pattern_cache(directory_service, fake_adapter) -> None:
    """Literal lookups must not occupy pattern-cache slots: a 10k-PV validate
    storm would otherwise evict every genuine pattern query."""
    fake_adapter.pvs = {"A:1", "B:1"}

    await directory_service.exists("A:1")
    await directory_service.exists("B:1")
    await directory_service.exists("MISSING:1")

    assert directory_service._pattern_cache.size() == 0
    assert directory_service._name_cache.size() == 3


@pytest.mark.asyncio
async def test_concurrent_exists_coalesce_to_one_upstream(make_directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"X:1"}
    fake_adapter.delay_s = 0.05
    service = make_directory_service(fake_adapter)

    results = await asyncio.gather(*(service.exists("X:1") for _ in range(20)))

    assert all(r is True for r in results)
    assert sum(1 for c in fake_adapter.calls if c[0] == "list_pvs") == 1


@pytest.mark.asyncio
async def test_exists_timeout_translates_to_directory_timeout(make_directory_service, fake_adapter) -> None:
    fake_adapter.delay_s = 0.5
    service = make_directory_service(fake_adapter, timeout_s=0.05)

    with pytest.raises(DirectoryTimeout):
        await service.exists("X:1")


@pytest.mark.asyncio
async def test_validate_duplicate_uncached_pvs_issue_one_upstream_call(directory_service, fake_adapter) -> None:
    fake_adapter.pvs = {"A:1"}

    result = await directory_service.validate(["A:1", "A:1", "B:1", "B:1"])

    assert result.known == ["A:1", "A:1"]
    assert result.unknown == ["B:1", "B:1"]
    assert sum(1 for c in fake_adapter.calls if c == ("list_pvs", "A:1")) == 1
    assert sum(1 for c in fake_adapter.calls if c == ("list_pvs", "B:1")) == 1


@pytest.mark.asyncio
async def test_stats_survives_health_probe_raising(directory_service, fake_adapter) -> None:
    """An adapter whose health() raises must degrade the summary, not 500 it."""

    async def exploding_health():
        raise RuntimeError("probe blew up")

    fake_adapter.health = exploding_health

    stats = await directory_service.stats()

    assert stats.adapter_health.healthy is False
    assert "probe blew up" in (stats.adapter_health.last_error or "")
