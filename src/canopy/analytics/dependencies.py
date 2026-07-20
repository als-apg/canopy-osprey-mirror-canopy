from typing import Annotated

from fastapi import Depends

from canopy.analytics.config import get_analytics_config
from canopy.analytics.services.analytics_service import AnalyticsService

_service: AnalyticsService | None = None


def get_analytics_service() -> AnalyticsService:
    global _service
    if _service is None:
        _service = AnalyticsService(get_analytics_config())
    return _service


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
