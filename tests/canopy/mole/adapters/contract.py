"""Reusable adapter conformance assertions.

Functions here exercise the `ArchiverAdapter` protocol's behavioral contract.
Pass any adapter; the assertions match the wiki design (`Mole-Module-Architecture.md`
§11–§12). Out-of-tree adapter authors can call these from their own test
suite to verify their implementation conforms.
"""

import pytest

from canopy.contracts.sample import AlarmSeverity
from canopy.mole.adapters.base import (
    AdapterHealth,
    ArchiverAdapter,
    BinningNotSupported,
)


async def assert_health(adapter: ArchiverAdapter) -> None:
    h = await adapter.health()
    assert isinstance(h, AdapterHealth)
    assert isinstance(h.healthy, bool)
    assert isinstance(h.last_check_ts_ns, int)


async def assert_close_idempotent(adapter: ArchiverAdapter) -> None:
    await adapter.close()
    await adapter.close()


async def assert_fetch_window_invariants(adapter: ArchiverAdapter, pv: str, t0_ns: int, t1_ns: int) -> None:
    samples = await adapter.fetch_window(pv, t0_ns=t0_ns, t1_ns=t1_ns)
    for s in samples:
        assert s.source == "archive", "Mole adapters must tag samples with source='archive'"
        assert isinstance(s.severity, AlarmSeverity)
    for prev, curr in zip(samples, samples[1:]):
        assert prev.ts_ns <= curr.ts_ns, "Samples must be time-ordered ascending"


async def assert_fetch_binned_auto(
    adapter: ArchiverAdapter, pv: str, t0_ns: int, t1_ns: int, target_buckets: int = 5
) -> None:
    try:
        points = await adapter.fetch_binned(
            pv,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
            binning="auto",
            target_buckets=target_buckets,
            bin_size_ns=None,
        )
    except BinningNotSupported:
        pytest.skip("adapter does not implement binning")
        return
    for p in points:
        assert p.count >= 0
        assert p.bin_ns > 0
    for prev, curr in zip(points, points[1:]):
        assert prev.ts_ns <= curr.ts_ns
