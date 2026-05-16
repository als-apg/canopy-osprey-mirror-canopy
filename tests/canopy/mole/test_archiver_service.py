"""ArchiverService unit tests — focus on the service-layer logic
(dispatch and decimation fallback) independent of any HTTP routing."""

from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.mole.config import MoleConfig
from canopy.mole.adapters.registry import AdapterRegistry
from canopy.mole.services.archiver_service import ArchiverService


class _IgnoreMaxAdapter:
    """Returns a fixed N samples regardless of max_points hint — exercises decimation."""

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
