"""Analytics domain exceptions.

The per-module exception handler (registered via
``register_canopy_error_handler``) maps them to ``{"code": ..., "detail": ...}``.
"""

from canopy.core.errors import ErrorCode, CanopyError


class AnalyticsError(CanopyError):
    """Base for all analytics errors."""


class EventTooLarge(AnalyticsError):
    """The event body exceeds ``AnalyticsConfig.max_event_bytes``."""

    status_code = 413
    error_code = ErrorCode.ANALYTICS_EVENT_TOO_LARGE
