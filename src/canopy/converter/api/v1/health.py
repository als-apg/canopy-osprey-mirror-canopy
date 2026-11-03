from fastapi import Security

from canopy.core.health import make_health_router
from canopy.core.security import require_read_access
from canopy.contracts.health import ConverterHealthSummaryDTO
from canopy.converter.runtime import uptime_s
from canopy.core.color_scheme import get_color_scheme
from canopy.converter.dependencies import ConverterConfigDep

router = make_health_router("converter", uptime_s)


@router.get("/summary", dependencies=[Security(require_read_access)])
def summary(config: ConverterConfigDep) -> ConverterHealthSummaryDTO:
    """The converter's fallbacks: ``color-scheme`` while its scheme file is rejected and ``canopy-default`` is served."""
    # The scheme the converter converts with: its own scheme_path, else CANOPY_THEME_FILE.
    causes = list(get_color_scheme(config.scheme_path).degraded)
    return ConverterHealthSummaryDTO(status="degraded" if causes else "ok", degraded=causes)
