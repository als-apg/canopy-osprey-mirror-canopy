"""Minimal example `ArchiverAdapter` for the dotted-path loading test + docs.

This is what an out-of-tree lab would write. It is *not* registered via the
`@register_archiver_type` decorator; it is loaded via the `module:Class`
config form, demonstrating the no-registration path.
"""

import time
from dataclasses import dataclass

from canopy.mole.config import ArchiverInstanceConfig
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import Sample, BinnedPoint, AlarmSeverity
from canopy.mole.adapters.base import (
    AdapterHealth,
    BinningNotSupported,
    PVNotFoundInArchiver,
)


@dataclass
class ExampleAdapter:
    """Static example: returns deterministic synthetic data.

    Real adapters implement the same interface against their own protocol
    (HTTP, gRPC, raw sockets, whatever).
    """

    name: str = "example"
    type_name: str = "example"

    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "ExampleAdapter":
        return cls(name=name)

    async def fetch_window(self, pv, t0_ns, t1_ns, *, max_points=None):
        if pv != "PV:EXAMPLE":
            raise PVNotFoundInArchiver(f"PV '{pv}' unknown to example adapter")
        n = 5 if (t1_ns - t0_ns) > 0 else 0
        step = max((t1_ns - t0_ns) // max(n, 1), 1)
        return [
            Sample(
                ts_ns=t0_ns + i * step,
                value=float(i),
                severity=AlarmSeverity.NO_ALARM,
                source="archive",
            )
            for i in range(n)
        ]

    async def fetch_last_before(self, pv, ts_ns):
        if pv != "PV:EXAMPLE":
            raise PVNotFoundInArchiver(f"PV '{pv}' unknown to example adapter")
        if ts_ns <= 0:
            return None
        return Sample(ts_ns=ts_ns - 1, value=-1.0, severity=AlarmSeverity.NO_ALARM, source="archive")

    async def fetch_binned(self, pv, t0_ns, t1_ns, *, binning, target_buckets, bin_size_ns):
        if pv != "PV:EXAMPLE":
            raise PVNotFoundInArchiver(f"PV '{pv}' unknown to example adapter")
        if binning == "auto":
            n = target_buckets or 1
            step = max((t1_ns - t0_ns) // n, 1)
            return [
                BinnedPoint(
                    ts_ns=t0_ns + i * step,
                    bin_ns=step,
                    mean=float(i),
                    std_dev=0.0,
                    min=float(i),
                    max=float(i),
                    count=1,
                    severity=AlarmSeverity.NO_ALARM,
                )
                for i in range(n)
            ]
        raise BinningNotSupported("example adapter only supports binning='auto'")

    async def metadata(self, pv: str) -> PVMetadata:
        if pv != "PV:EXAMPLE":
            raise PVNotFoundInArchiver(f"PV '{pv}' unknown to example adapter")
        return PVMetadata(pv=pv, archiver=self.name, units="example", precision=0)

    async def search(self, pattern: str, *, limit: int) -> list[str]:
        return ["PV:EXAMPLE"][:limit]

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            healthy=True,
            latency_ms=0.0,
            last_error=None,
            last_check_ts_ns=time.time_ns(),
        )

    async def close(self) -> None:
        pass
