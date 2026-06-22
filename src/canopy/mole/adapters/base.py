"""Adapter contract + domain exception hierarchy for Mole.

`ArchiverAdapter` is a Protocol; concrete adapters (Appliance, out-of-tree
labs') implement it. `register_archiver_type` registers a short name; the
`module:Class` dotted-path form in config is resolved at registry-build time
without needing prior registration.

Severity normalization (`normalize_severity`) lives here so all adapters share
one mapping (per §12.1 of `Mole-Module-Architecture.md`).
"""

from typing import Literal, Protocol, runtime_checkable

from canopy.core.errors import ErrorCode, CanopyError
from canopy.mole.config import ArchiverInstanceConfig
from canopy.contracts.base import CanopyModel
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import Sample, BinnedPoint, AlarmSeverity

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
