"""Snapshot DTOs."""

from typing import Any
from datetime import datetime

from pydantic import Field, ConfigDict

from canopy.contracts.base import CanopyModel


class EpicsValueDTO(CanopyModel):
    """A stored EPICS value, read back out of the ``snapshot_value`` JSONB blob.

    ``extra="ignore"`` tolerates blob keys the DTO doesn't model (forward-compat as
    snapshot metadata fidelity grows).
    """

    model_config = ConfigDict(extra="ignore")

    value: Any = None
    status: int | None = None
    severity: int | None = None
    timestamp: datetime | None = None
    units: str | None = None
    precision: int | None = None
    upper_ctrl_limit: float | None = None
    lower_ctrl_limit: float | None = None


class TagInfoDTO(CanopyModel):
    """Lightweight tag info attached to a snapshot value."""

    id: str
    name: str
    group_name: str


class PVValueDTO(CanopyModel):
    """One PV's value within a snapshot (setpoint + readback)."""

    pv_id: str
    pv_name: str  # Primary name (setpoint or readback)
    device: str | None = None
    setpoint_name: str | None = None
    readback_name: str | None = None
    setpoint_value: EpicsValueDTO | None = None
    readback_value: EpicsValueDTO | None = None
    status: int | None = None
    severity: int | None = None
    timestamp: datetime | None = None
    tags: list[TagInfoDTO] = []


class MetaPVValueDTO(EpicsValueDTO):
    """A meta-PV's value within a snapshot."""

    pv_id: str
    pv_name: str  # Setpoint address if present, else readback address


class SnapshotBase(CanopyModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class NewSnapshotDTO(SnapshotBase):
    """Create a snapshot. All registered PVs are captured automatically."""


class SnapshotSummaryDTO(SnapshotBase):
    """Summary DTO for listing snapshots."""

    id: str
    created_date: datetime
    created_by: str | None = None
    pv_count: int = 0
    meta_pvs: list[MetaPVValueDTO] = Field(default_factory=list)


class SnapshotDTO(SnapshotSummaryDTO):
    """Full snapshot with all PV values."""

    pv_values: list[PVValueDTO] = []


class UpdateSnapshotDTO(CanopyModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None


class RestoreRequestDTO(CanopyModel):
    """Request to restore PV values from a snapshot (``pv_ids=None`` restores all)."""

    pv_ids: list[str] | None = None


class RestoreResultDTO(CanopyModel):
    """Result of a restore operation.

    ``failures`` rows are ``{pvId, pvName, error, phase}``, where ``phase`` names the
    stage that failed: ``"Setpoints"`` for a primary write or a hook-derivation error,
    otherwise the class name of the first restore hook whose write failed. Class names
    map to the ``CANOPY_SQUIRREL__RESTORE_HOOKS`` provider keys as ``MagnetTrimHook`` →
    ``magnet_trim`` and ``WriteToConHook`` → ``write_to_con``; ``error`` lists every
    hook failure for the PV.
    """

    total_pvs: int
    success_count: int
    failure_count: int
    failures: list[dict] = []


class ComparisonResultDTO(CanopyModel):
    """Comparison between two stored snapshots.

    ``differences`` rows are ``{pvId, pvName, value1, value2, withinTolerance}``.
    """

    snapshot1_id: str
    snapshot2_id: str
    differences: list[dict] = []
    match_count: int
    difference_count: int
