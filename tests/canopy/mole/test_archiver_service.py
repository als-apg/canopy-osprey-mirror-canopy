"""ArchiverService unit tests: focus on the service-layer logic
(dispatch, decimation fallback, last-before prepend) independent of any HTTP routing."""

import pytest

from canopy.mole.config import MoleConfig
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.mole.adapters.base import LastBeforeNotSupported
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.services.archiver_service import ArchiverService


class _IgnoreMaxAdapter:
    """Returns a fixed N samples regardless of max_points hint, exercises decimation."""

    name = "raw"
    type_name = "raw"

    def __init__(self, n: int = 100) -> None:
        self._n = n

    async def fetch_window(self, pv, t0_ns, t1_ns, *, max_points=None):
        return [
            Sample(
                ts_ns=t0_ns + i * 10,
                value=float(i),
                severity=AlarmSeverity.NO_ALARM,
                source="archive",
            )
            for i in range(self._n)
        ]


class _LastBeforeAdapter(_IgnoreMaxAdapter):
    """Adds a configurable `fetch_last_before` on top of `_IgnoreMaxAdapter`."""

    def __init__(self, n: int = 100, prior: Sample | None = None) -> None:
        super().__init__(n)
        self.prior = prior
        self.last_before_calls: list[int] = []

    async def fetch_last_before(self, pv, ts_ns):
        self.last_before_calls.append(ts_ns)
        return self.prior


def _prior_sample(ts_ns: int) -> Sample:
    return Sample(ts_ns=ts_ns, value=-1.0, severity=AlarmSeverity.NO_ALARM, source="archive")


def _service(adapter, default="raw") -> ArchiverService:
    registry = AdapterRegistry({adapter.name: adapter}, default=default)
    return ArchiverService(registry, MoleConfig(default_archiver=default))


async def test_fetch_window_decimation_engages():
    adapter = _IgnoreMaxAdapter(n=100)
    service = _service(adapter)
    name, samples, decimated = await service.fetch_window("PV:X", 0, 10_000, max_points=10)
    assert name == "raw"
    assert len(samples) <= 10
    assert decimated is True


async def test_fetch_window_decimation_skipped_when_not_needed():
    adapter = _IgnoreMaxAdapter(n=5)
    service = _service(adapter)
    _, samples, decimated = await service.fetch_window("PV:X", 0, 10_000, max_points=10)
    assert len(samples) == 5
    assert decimated is False


async def test_fetch_window_no_max_points_no_decimation():
    adapter = _IgnoreMaxAdapter(n=100)
    service = _service(adapter)
    _, samples, decimated = await service.fetch_window("PV:X", 0, 10_000, max_points=None)
    assert len(samples) == 100
    assert decimated is False


# --------------------------------------------------------------------------- #
# include_last_before
# --------------------------------------------------------------------------- #


async def test_include_last_before_prepends_prior_sample():
    adapter = _LastBeforeAdapter(n=5, prior=_prior_sample(900))
    service = _service(adapter)
    _, samples, _ = await service.fetch_window("PV:X", 1_000, 10_000, include_last_before=True)
    assert len(samples) == 6
    assert samples[0].ts_ns == 900
    assert samples[0].value == -1.0
    # Strictly-before semantics: the adapter is asked for t0_ns - 1.
    assert adapter.last_before_calls == [999]


async def test_include_last_before_no_prior_sample_is_omitted():
    adapter = _LastBeforeAdapter(n=5, prior=None)
    service = _service(adapter)
    _, samples, _ = await service.fetch_window("PV:X", 1_000, 10_000, include_last_before=True)
    assert len(samples) == 5
    assert samples[0].ts_ns == 1_000


async def test_include_last_before_prior_at_window_start_not_duplicated():
    """A defensive adapter returning a sample at/after t0 must not produce a duplicate."""
    adapter = _LastBeforeAdapter(n=5, prior=_prior_sample(1_000))
    service = _service(adapter)
    _, samples, _ = await service.fetch_window("PV:X", 1_000, 10_000, include_last_before=True)
    assert len(samples) == 5
    assert samples[0].value == 0.0


async def test_include_last_before_prior_exempt_from_decimation():
    adapter = _LastBeforeAdapter(n=100, prior=_prior_sample(900))
    service = _service(adapter)
    _, samples, decimated = await service.fetch_window("PV:X", 1_000, 10_000, max_points=10, include_last_before=True)
    assert decimated is True
    assert samples[0].ts_ns == 900  # prior survives decimation...
    assert len(samples) <= 11  # ...and max_points bounds only the in-window samples


async def test_include_last_before_unsupported_adapter_raises():
    adapter = _IgnoreMaxAdapter(n=5)  # has no fetch_last_before
    service = _service(adapter)
    with pytest.raises(LastBeforeNotSupported):
        await service.fetch_window("PV:X", 1_000, 10_000, include_last_before=True)


async def test_fetch_window_default_does_not_call_last_before():
    adapter = _LastBeforeAdapter(n=5, prior=_prior_sample(900))
    service = _service(adapter)
    _, samples, _ = await service.fetch_window("PV:X", 1_000, 10_000)
    assert len(samples) == 5
    assert adapter.last_before_calls == []
