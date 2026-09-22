"""Typed ``Annotated[..., Depends(...)]`` aliases for the analytics API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends, Request

from canopy.analytics import gateway
from canopy.analytics.config import AnalyticsConfig, get_analytics_config
from canopy.analytics.errors import EventTooLarge
from canopy.analytics.services.analytics_service import AnalyticsService


def get_analytics_service() -> AnalyticsService:
    return gateway.get_service()


def get_config() -> AnalyticsConfig:
    return get_analytics_config()


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
AnalyticsConfigDep = Annotated[AnalyticsConfig, Depends(get_config)]


async def enforce_event_size(request: Request, config: AnalyticsConfigDep) -> None:
    """Reject an event body over ``max_event_bytes`` before it is recorded.

    FastAPI has already buffered the body by the time dependencies run, so this
    guards what reaches the log file (and Loki, whose line cap would otherwise
    drop the event silently after the API returned 204), not process memory.
    The body is cached on the request, so reading it again costs nothing.
    """
    size = len(await request.body())
    if size > config.max_event_bytes:
        raise EventTooLarge(f"Event is {size} bytes; limit is {config.max_event_bytes} bytes")
