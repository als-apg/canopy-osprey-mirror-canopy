"""Public surface of the analytics module for in-process callers.

Other Canopy modules import only from this file; the rest of
``canopy.analytics.*`` is private implementation. ``bind()`` wires the service
singleton at app startup; ``get_service()`` is exposed for ``dependencies.py``.
Tests can re-bind a fake service freely.
"""

from canopy.analytics.services.analytics_service import AnalyticsService

_service: AnalyticsService | None = None


def bind(service: AnalyticsService | None) -> None:
    """Wire (or unwire) the AnalyticsService singleton."""
    global _service
    _service = service


def get_service() -> AnalyticsService:
    if _service is None:
        raise RuntimeError("Analytics gateway accessed before app startup bound a service")
    return _service
