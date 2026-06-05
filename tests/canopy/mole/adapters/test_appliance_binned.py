"""EpicsArchiverApplianceAdapter: `fetch_binned` (auto + time)."""

import pytest
from pytest_httpx import HTTPXMock

from canopy.mole.adapters.base import ArchiverBadResponse, BinningNotSupported
from canopy.mole.adapters.appliance import EpicsArchiverApplianceAdapter


@pytest.fixture
async def adapter():
    a = EpicsArchiverApplianceAdapter(
        name="test",
        url="https://archappl.example/retrieval",
        timeout_s=5.0,
    )
    yield a
    await a.close()


def _optimized_response(rows):
    return [
        {
            "meta": {"name": "PV:TEST", "EGU": "V"},
            "data": rows,
        }
    ]


# --------------------------------------------------------------------------- #
# auto mode
# --------------------------------------------------------------------------- #


async def test_fetch_binned_auto_uses_optimized_operator(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json=_optimized_response(
            [
                {"secs": 0, "nanos": 0, "val": [1.0, 0.1, 0.5, 1.5, 100], "severity": 0, "status": 0},
                {"secs": 5, "nanos": 0, "val": [2.0, 0.2, 1.5, 2.5, 200], "severity": 0, "status": 0},
            ]
        )
    )
    points = await adapter.fetch_binned(
        "PV:TEST",
        t0_ns=0,
        t1_ns=10_000_000_000,
        binning="auto",
        target_buckets=2,
        bin_size_ns=None,
    )
    request = httpx_mock.get_request()
    assert request.url.params["pv"] == "optimized_2(PV:TEST)"

    assert len(points) == 2
    p = points[0]
    assert p.mean == 1.0
    assert p.std_dev == 0.1
    assert p.min == 0.5
    assert p.max == 1.5
    assert p.count == 100
    # bin_ns from gap to next bucket
    assert p.bin_ns == 5_000_000_000
    assert points[1].bin_ns == 0  # last bucket


async def test_fetch_binned_auto_missing_target_raises(adapter):
    with pytest.raises(BinningNotSupported):
        await adapter.fetch_binned("PV:TEST", 0, 1_000, binning="auto", target_buckets=None, bin_size_ns=None)


async def test_fetch_binned_auto_zero_target_raises(adapter):
    with pytest.raises(BinningNotSupported):
        await adapter.fetch_binned("PV:TEST", 0, 1_000, binning="auto", target_buckets=0, bin_size_ns=None)


# --------------------------------------------------------------------------- #
# time mode
# --------------------------------------------------------------------------- #


async def test_fetch_binned_time_maps_to_optimized(adapter, httpx_mock: HTTPXMock):
    """time mode computes N from window/binSize and calls optimized_<N>."""
    httpx_mock.add_response(
        json=_optimized_response(
            [{"secs": i, "nanos": 0, "val": [1.0, 0, 0.5, 1.5, 10], "severity": 0, "status": 0} for i in range(10)]
        )
    )
    # Window is 10 seconds, binSize is 1 second → N=10
    await adapter.fetch_binned(
        "PV:TEST",
        t0_ns=0,
        t1_ns=10_000_000_000,
        binning="time",
        target_buckets=None,
        bin_size_ns=1_000_000_000,
    )
    request = httpx_mock.get_request()
    assert request.url.params["pv"] == "optimized_10(PV:TEST)"


async def test_fetch_binned_time_missing_bin_size_raises(adapter):
    with pytest.raises(BinningNotSupported):
        await adapter.fetch_binned("PV:TEST", 0, 10_000, binning="time", target_buckets=None, bin_size_ns=None)


# --------------------------------------------------------------------------- #
# parse error cases
# --------------------------------------------------------------------------- #


async def test_fetch_binned_short_val_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json=_optimized_response([{"secs": 0, "nanos": 0, "val": [1.0, 0.1], "severity": 0, "status": 0}])
    )
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_binned("PV:TEST", 0, 10, binning="auto", target_buckets=2, bin_size_ns=None)


async def test_fetch_binned_scalar_val_promotes_to_degenerate_bin(adapter, httpx_mock: HTTPXMock):
    """When optimized_N falls back to a scalar val (too few samples to bin), produce a 1-count bin."""
    httpx_mock.add_response(
        json=_optimized_response(
            [
                {"secs": 0, "nanos": 0, "val": 3.7841796875e-09, "severity": 0, "status": 0},
                {"secs": 5, "nanos": 0, "val": [2.0, 0.2, 1.5, 2.5, 200], "severity": 0, "status": 0},
            ]
        )
    )
    points = await adapter.fetch_binned(
        "PV:TEST", 0, 10_000_000_000, binning="auto", target_buckets=2, bin_size_ns=None
    )
    assert len(points) == 2
    p = points[0]
    assert p.mean == 3.7841796875e-09
    assert p.std_dev == 0.0
    assert p.min == 3.7841796875e-09
    assert p.max == 3.7841796875e-09
    assert p.count == 1


async def test_fetch_binned_unparseable_val_raises_bad_response(adapter, httpx_mock: HTTPXMock):
    """Anything that's neither a number nor a 5-element list is a real protocol error."""
    httpx_mock.add_response(
        json=_optimized_response([{"secs": 0, "nanos": 0, "val": "oops", "severity": 0, "status": 0}])
    )
    with pytest.raises(ArchiverBadResponse):
        await adapter.fetch_binned("PV:TEST", 0, 10, binning="auto", target_buckets=2, bin_size_ns=None)


async def test_fetch_binned_unknown_mode_raises(adapter):
    with pytest.raises(BinningNotSupported):
        await adapter.fetch_binned("PV:TEST", 0, 10, binning="magical", target_buckets=1, bin_size_ns=None)
