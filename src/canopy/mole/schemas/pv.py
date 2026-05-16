from canopy.contracts.base import CanopyModel


class PVMetadata(CanopyModel):
    """Per-PV metadata as known to one archiver.

    Defined as a superset of what any single archiver provides. The Appliance
    adapter populates only `pv`, `archiver`, `units` (EGU), and `precision`
    (PREC); other fields are `null` and come from Channel Access (Hare / EPICS
    Connector). Other adapter types may populate more.
    """

    pv: str
    archiver: str
    description: str | None = None
    units: str | None = None
    precision: int | None = None
    display_low: float | None = None
    display_high: float | None = None
    alarm_low: float | None = None
    alarm_high: float | None = None
    warning_low: float | None = None
    warning_high: float | None = None


# Wire DTO is the same shape; alias kept for clarity at the API layer.
PVMetadataDTO = PVMetadata
