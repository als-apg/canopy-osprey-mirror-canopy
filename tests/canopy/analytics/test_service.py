"""AnalyticsService unit behavior — fire-and-forget guarantees."""

import pytest

from canopy.core.security import APIKey
from canopy.analytics.config import AnalyticsConfig
from canopy.analytics.services import analytics_service as service_module
from canopy.analytics.schemas.events import AnalyticsEventDTO
from canopy.analytics.services.analytics_service import AnalyticsService, _scopes


def test_record_never_raises_even_if_logging_fails(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("logging backend down")

    monkeypatch.setattr(service_module.logger, "info", explode)
    service = AnalyticsService(AnalyticsConfig(enabled=True))
    service.record(AnalyticsEventDTO(event="x"))  # must not propagate


@pytest.mark.parametrize(
    ("read", "write", "expected"),
    [(True, False, "read"), (False, True, "write"), (True, True, "read+write")],
)
def test_scopes_rendering(read, write, expected):
    assert _scopes(APIKey(token="t", read=read, write=write)) == expected


def test_scopes_none_for_anonymous():
    assert _scopes(None) is None
