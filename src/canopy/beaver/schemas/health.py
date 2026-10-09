from typing import Literal

from canopy.contracts.base import CanopyModel


class HealthSummaryDTO(CanopyModel):
    """Catalog rollup returned by ``/health/summary``.

    ``degraded`` means the catalog is empty — the loader tolerates a missing
    ``widgets_dir`` (serving no widgets) rather than failing the composed
    app's startup, and this is where that state surfaces.
    """

    status: Literal["ok", "degraded"]
    widget_count: int
    category_count: int
    registry_version: str
