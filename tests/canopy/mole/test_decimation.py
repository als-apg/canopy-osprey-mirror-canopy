"""Property + edge-case tests for uniform_time_decimate."""

from hypothesis import given
from hypothesis import strategies as st

from canopy.core.decimation import uniform_time_decimate
from canopy.contracts.sample import Sample, AlarmSeverity


def _sample(ts: int, val: float = 0.0) -> Sample:
    return Sample(ts_ns=ts, value=val, severity=AlarmSeverity.NO_ALARM, source="archive")


@given(
    n=st.integers(min_value=0, max_value=200),
    max_points=st.integers(min_value=0, max_value=50),
)
def test_decimate_never_exceeds_max_points(n, max_points):
    samples = [_sample(i * 1000) for i in range(n)]
    out = uniform_time_decimate(samples, max_points)
    assert len(out) <= max(max_points, 0)


@given(n=st.integers(min_value=1, max_value=100))
def test_decimate_passthrough_when_input_smaller(n):
    samples = [_sample(i * 1000) for i in range(n)]
    out = uniform_time_decimate(samples, max_points=n + 10)
    assert out == samples


def test_decimate_zero_max_points_returns_empty():
    samples = [_sample(i) for i in range(10)]
    assert uniform_time_decimate(samples, 0) == []


def test_decimate_one_max_point_returns_first():
    samples = [_sample(i) for i in range(10)]
    assert uniform_time_decimate(samples, 1) == [samples[0]]


def test_decimate_preserves_ordering():
    samples = [_sample(i * 100) for i in range(100)]
    out = uniform_time_decimate(samples, 10)
    for prev, curr in zip(out, out[1:]):
        assert prev.ts_ns < curr.ts_ns


def test_decimate_all_same_timestamp_returns_first():
    samples = [_sample(1000) for _ in range(10)]
    out = uniform_time_decimate(samples, 5)
    assert out == [samples[0]]


def test_decimate_includes_endpoints():
    """Endpoints should appear when max_points >= 2."""
    samples = [_sample(i * 100) for i in range(100)]
    out = uniform_time_decimate(samples, 5)
    assert out[0].ts_ns == samples[0].ts_ns
    assert out[-1].ts_ns == samples[-1].ts_ns
