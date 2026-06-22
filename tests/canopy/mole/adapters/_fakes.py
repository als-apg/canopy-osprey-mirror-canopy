"""In-memory deterministic adapter for tests."""

import re
import time
from dataclasses import field, dataclass

from canopy.mole.config import ArchiverInstanceConfig
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import Sample, BinnedPoint, AlarmSeverity
from canopy.mole.adapters.base import (
    AdapterHealth,
    BinningNotSupported,
    PVNotFoundInArchiver,
    register_archiver_type,
)


@register_archiver_type("fake")
@dataclass
class FakeArchiverAdapter:
    """Predictable adapter for tests.

    Returns synthetic samples with timestamps evenly spread across the requested
    window. ``pvs`` is the in-memory PV index; PVs missing from this dict raise
    ``PVNotFoundInArchiver``.
    """

    name: str = "fake"
    type_name: str = "fake"
    pvs: dict[str, dict] = field(
        default_factory=lambda: {
            "PV:TEST": {"units": "V", "precision": 3},
            "TEST:COUNTER": {"units": "counts", "precision": 0},
            "TEST:SINE": {"units": "", "precision": 4},
            "TEST:CONST": {"units": "", "precision": 1},
        }
    )
    closed: bool = False

    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "FakeArchiverAdapter":
        return cls(name=name)

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        max_points: int | None = None,
    ) -> list[Sample]:
        if pv not in self.pvs:
            raise PVNotFoundInArchiver(f"PV '{pv}' not in fake archiver")
        n = max_points or 10
        if t1_ns <= t0_ns:
            return []
        step = max((t1_ns - t0_ns) // n, 1)
        return [
            Sample(
                ts_ns=t0_ns + i * step,
                value=float(i),
                severity=AlarmSeverity.NO_ALARM,
                source="archive",
            )
            for i in range(n)
        ]

    async def fetch_last_before(self, pv: str, ts_ns: int) -> Sample | None:
        """Deterministic prior sample: 1 s before `ts_ns`, value -1.0."""
        if pv not in self.pvs:
            raise PVNotFoundInArchiver(f"PV '{pv}' not in fake archiver")
        if ts_ns <= 0:
            return None
        return Sample(
            ts_ns=ts_ns - 1_000_000_000,
            value=-1.0,
            severity=AlarmSeverity.NO_ALARM,
            source="archive",
        )

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning,
        target_buckets,
        bin_size_ns,
    ) -> list[BinnedPoint]:
        if pv not in self.pvs:
            raise PVNotFoundInArchiver(f"PV '{pv}' not in fake archiver")
        if binning == "auto":
            buckets = target_buckets or 5
            step = max((t1_ns - t0_ns) // buckets, 1)
            return [
                BinnedPoint(
                    ts_ns=t0_ns + i * step,
                    bin_ns=step,
                    mean=float(i),
                    std_dev=0.1,
                    min=float(i) - 0.1,
                    max=float(i) + 0.1,
                    count=10,
                    severity=AlarmSeverity.NO_ALARM,
                )
                for i in range(buckets)
            ]
        if binning == "time":
            if bin_size_ns is None:
                raise ValueError("binning=time requires bin_size_ns")
            buckets = max((t1_ns - t0_ns) // bin_size_ns, 1)
            return [
                BinnedPoint(
                    ts_ns=t0_ns + i * bin_size_ns,
                    bin_ns=bin_size_ns,
                    mean=float(i),
                    std_dev=0.1,
                    min=float(i) - 0.1,
                    max=float(i) + 0.1,
                    count=10,
                    severity=AlarmSeverity.NO_ALARM,
                )
                for i in range(buckets)
            ]
        raise BinningNotSupported(f"binning={binning} not supported in fake")

    async def metadata(self, pv: str) -> PVMetadata:
        if pv not in self.pvs:
            raise PVNotFoundInArchiver(f"PV '{pv}' not in fake archiver")
        meta = self.pvs[pv]
        return PVMetadata(
            pv=pv,
            archiver=self.name,
            units=meta.get("units"),
            precision=meta.get("precision"),
        )

    async def search(self, pattern: str, *, limit: int) -> list[str]:
        try:
            regex = re.compile(pattern)
        except re.error:
            return []
        return [pv for pv in self.pvs if regex.search(pv)][:limit]

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            healthy=not self.closed,
            latency_ms=0.1,
            last_error=None,
            last_check_ts_ns=time.time_ns(),
        )

    async def close(self) -> None:
        self.closed = True
