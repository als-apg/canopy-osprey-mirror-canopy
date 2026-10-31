"""Adapter contract + domain exception hierarchy for Mole.

`ArchiverAdapter` is a Protocol; concrete adapters (Appliance, out-of-tree
labs') implement it. `register_archiver_type` registers a short name; the
`module:Class` dotted-path form in config is resolved at registry-build time
without needing prior registration.

Severity normalization (`normalize_severity`) lives here so all adapters share
one mapping (per §12.1 of `Mole-Module-Architecture.md`).
"""

from typing import Literal, Protocol, runtime_checkable
from dataclasses import dataclass

import numpy as np

from canopy.core.errors import ErrorCode, CanopyError
from canopy.mole.config import ArchiverInstanceConfig
from canopy.contracts.base import CanopyModel
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import SEVERITY_ORDINAL, Sample, BinnedPoint, AlarmSeverity

# --------------------------------------------------------------------------- #
# Domain exceptions, services raise these; the per-module handler maps them
# to {"code": <slug>, "detail": ...} JSON.
# --------------------------------------------------------------------------- #


class ArchiverError(CanopyError):
    """Base for all archiver-related errors."""


class ArchiverNotConfigured(ArchiverError):
    status_code = 404
    error_code = ErrorCode.ARCHIVER_NOT_CONFIGURED


class ArchiverUnavailable(ArchiverError):
    status_code = 503
    error_code = ErrorCode.ARCHIVER_UNAVAILABLE


class ArchiverTimeout(ArchiverUnavailable):
    status_code = 504
    error_code = ErrorCode.ARCHIVER_TIMEOUT


class ArchiverBadResponse(ArchiverError):
    status_code = 502
    error_code = ErrorCode.ARCHIVER_BAD_RESPONSE


class PVNotFoundInArchiver(ArchiverError):
    status_code = 404
    error_code = ErrorCode.PV_NOT_FOUND_IN_ARCHIVER


class BinningNotSupported(ArchiverError):
    status_code = 501
    error_code = ErrorCode.BINNING_NOT_SUPPORTED


class LastBeforeNotSupported(ArchiverError):
    status_code = 501
    error_code = ErrorCode.LAST_BEFORE_NOT_SUPPORTED


# --------------------------------------------------------------------------- #
# Severity normalization
# --------------------------------------------------------------------------- #

_SEVERITY_MAP: dict[int, AlarmSeverity] = {
    0: AlarmSeverity.NO_ALARM,
    1: AlarmSeverity.MINOR,
    2: AlarmSeverity.MAJOR,
    3: AlarmSeverity.INVALID,
}


def normalize_severity(value: int | None) -> AlarmSeverity:
    """Map an Appliance-style integer severity to Canopy's enum.

    Unknown codes (including ``None``) → ``INVALID`` per §12.1 of the design.
    """
    if value is None:
        return AlarmSeverity.INVALID
    return _SEVERITY_MAP.get(value, AlarmSeverity.INVALID)


# --------------------------------------------------------------------------- #
# Budgeted seed fetch
# --------------------------------------------------------------------------- #

#: Target samples per archive request on the seed path; the next window is sized to it.
SEED_SAMPLE_BUDGET = 20_000
#: The first (newest) window of a budgeted fetch, before any rate is known.
FIRST_SEED_WINDOW_NS = 60 * 1_000_000_000
#: ``fetch_last_before`` accepts at most this many timestamps per call ...
LAST_BEFORE_MAX_TIMESTAMPS = 500
#: ... and runs at most this many lookups at once.
LAST_BEFORE_CONCURRENCY = 8


@dataclass(frozen=True)
class ArchiveBlock:
    """Archive samples as parallel arrays, ascending in time, built off the event loop.

    ``severities`` are ``SEVERITY_ORDINAL`` codes; ``values`` is an object array
    (raw archive values, numeric or not).
    """

    ts_ns: np.ndarray  # int64
    values: np.ndarray  # object
    severities: np.ndarray  # int8

    def __len__(self) -> int:
        return int(self.ts_ns.size)

    @classmethod
    def empty(cls) -> "ArchiveBlock":
        return cls(np.empty(0, dtype=np.int64), np.empty(0, dtype=object), np.empty(0, dtype=np.int8))

    @classmethod
    def from_samples(cls, samples: list[Sample]) -> "ArchiveBlock":
        """Build from ``Sample`` objects (a fallback for adapters without a budgeted fetch)."""
        ordered = sorted(samples, key=lambda s: s.ts_ns)
        values = np.empty(len(ordered), dtype=object)
        for i, s in enumerate(ordered):
            values[i] = s.value
        return cls(
            np.fromiter((s.ts_ns for s in ordered), dtype=np.int64, count=len(ordered)),
            values,
            np.fromiter((SEVERITY_ORDINAL[s.severity] for s in ordered), dtype=np.int8, count=len(ordered)),
        )

    @staticmethod
    def concat(blocks: list["ArchiveBlock"]) -> "ArchiveBlock":
        """Merge blocks into one ascending block, keeping one sample per timestamp."""
        blocks = [b for b in blocks if len(b)]
        if not blocks:
            return ArchiveBlock.empty()
        ts = np.concatenate([b.ts_ns for b in blocks])
        values = np.concatenate([b.values for b in blocks])
        severities = np.concatenate([b.severities for b in blocks])
        order = np.argsort(ts, kind="stable")
        ts, values, severities = ts[order], values[order], severities[order]
        keep = np.concatenate(([True], ts[1:] != ts[:-1]))
        return ArchiveBlock(ts[keep], values[keep], severities[keep])


#: Largest factor one budgeted-fetch step may widen its window by.
WINDOW_GROWTH_MAX = 8


async def fetch_window_budgeted_via(
    fetch_block,
    pv: str,
    t0_ns: int,
    t1_ns: int,
    *,
    budget: int,
    first_window_ns: int = FIRST_SEED_WINDOW_NS,
) -> ArchiveBlock:
    """Walk ``[t0_ns, t1_ns]`` newest-first in windows sized from the observed rate.

    ``fetch_block(pv, lo, hi, include_last_before)`` returns one ``ArchiveBlock``
    for ``[lo, hi]``, or ``None`` to ask for a narrower window (the adapter hit a
    server cap); the oldest window also carries the last sample before it. The
    next window holds ``budget`` samples at the rate just observed -- a response
    over the budget shrinks it proportionally -- and is never grown by doubling.
    Growth per step is capped at ``WINDOW_GROWTH_MAX``x, so a sparse (or empty)
    recent window cannot balloon the next request over a dense older region.
    """
    if t1_ns < t0_ns:
        return ArchiveBlock.empty()
    blocks: list[ArchiveBlock] = []
    hi = t1_ns
    width = max(1, first_window_ns)
    while True:
        lo = max(t0_ns, hi - width)
        block = await fetch_block(pv, lo, hi, lo == t0_ns)
        if block is None:  # server cap: halve and retry the same end
            width = max(1, width // 2)
            continue
        blocks.append(block)
        if lo == t0_ns:
            break
        elapsed = hi - lo + 1
        width = max(1, min((elapsed * budget) // max(len(block), 1), (hi - lo) * WINDOW_GROWTH_MAX))
        hi = lo - 1
    return ArchiveBlock.concat(blocks)


# --------------------------------------------------------------------------- #
# Adapter health DTO
# --------------------------------------------------------------------------- #


class AdapterHealth(CanopyModel):
    healthy: bool
    latency_ms: float | None = None
    last_error: str | None = None
    last_check_ts_ns: int


# --------------------------------------------------------------------------- #
# Adapter Protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class ArchiverAdapter(Protocol):
    """Contract every archiver adapter must satisfy.

    See `Mole-Module-Architecture.md` §12 for the full specification.
    """

    name: str
    type_name: str

    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "ArchiverAdapter":
        """Build an instance from validated config; owns its own httpx client."""
        ...

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        max_points: int | None = None,
    ) -> list[Sample]:
        """Return raw samples in [t0_ns, t1_ns]. May ignore max_points; service decimates."""
        ...

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning: Literal["auto", "time"],
        target_buckets: int | None,
        bin_size_ns: int | None,
    ) -> list[BinnedPoint]:
        """Return statistical buckets in the requested mode.

        Adapters that legitimately cannot implement a mode raise
        `BinningNotSupported` (the registry surfaces this as 501).
        """
        ...

    async def fetch_last_before(self, pv: str, ts_ns: int) -> "Sample | None":
        """Return the most recent stored sample at or before `ts_ns`, or `None`.

        Backs the `includeLastBefore` flag on `/mole/v1/data`: for sparse
        on-change PVs, the value the PV held at a window's start is by
        definition the last sample *before* the window. The sample keeps its
        true timestamp — callers clip on render.

        Adapters whose backend cannot answer point-in-time lookups raise
        `LastBeforeNotSupported` (surfaced as 501).
        """
        ...

    async def metadata(self, pv: str) -> PVMetadata: ...

    async def search(self, pattern: str, *, limit: int) -> list[str]: ...

    async def health(self) -> AdapterHealth:
        """Cheap reachability check — typically a single status endpoint hit."""
        ...

    async def close(self) -> None:
        """Drain the underlying client. Called from app.lifespan shutdown."""
        ...


@runtime_checkable
class SeedCapableAdapter(Protocol):
    """Optional members an adapter adds for the live-table seed path.

    Neither is required, and each is looked up on its own: the archiver service
    serves an adapter without ``fetch_window_budgeted`` from ``fetch_window``,
    and counts an adapter without ``event_faithful`` as event-faithful.
    """

    #: True when the archive stores every change event with its severity (the
    #: Archiver Appliance); False for a polled recorder that writes every channel
    #: into each time slot with no alarm state. Lets a reader tell a polled
    #: archive from an event archive.
    event_faithful: bool

    async def fetch_window_budgeted(
        self, pv: str, t0_ns: int, t1_ns: int, *, budget: int = SEED_SAMPLE_BUDGET
    ) -> ArchiveBlock:
        """Raw samples in ``[t0_ns, t1_ns]`` as arrays, fetched newest-first in rate-sized windows.

        Every response is parsed off the event loop; the oldest window also
        carries the last sample before ``t0_ns``.
        """
        ...


# --------------------------------------------------------------------------- #
# Type registration: short-name → class. Out-of-tree adapters can skip this
# and use the `module:Class` dotted-path form in config instead.
# --------------------------------------------------------------------------- #

_REGISTERED_TYPES: dict[str, type] = {}


def register_archiver_type(name: str):
    """Decorator that registers an adapter class under a short type name.

    Example::

        @register_archiver_type("epics_archiver_appliance")
        class EpicsArchiverApplianceAdapter:
            ...

    Then ``cfg.type = "epics_archiver_appliance"`` resolves to this class.
    """

    def deco(cls: type) -> type:
        cls.type_name = name
        _REGISTERED_TYPES[name] = cls
        return cls

    return deco


def get_registered_type(name: str) -> type | None:
    return _REGISTERED_TYPES.get(name)
