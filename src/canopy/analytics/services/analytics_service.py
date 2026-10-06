"""Analytics event recording — a structured log line per event.

Events are emitted through the ``canopy.analytics`` logger with every field as
an ``extra`` attribute, so with ``CANOPY_LOG_FORMAT=json`` each event becomes
one JSON line that Promtail/Loki index directly (no payload-in-message
double encoding). There is deliberately no database: retention and querying
live in Loki (see docs/observability/).
"""

import logging
from typing import Any
from datetime import UTC, datetime

from canopy.core.security import APIKey
from canopy.analytics.config import AnalyticsConfig
from canopy.analytics.schemas.events import AnalyticsEventDTO

logger = logging.getLogger("canopy.analytics")


class AnalyticsService:
    def __init__(self, config: AnalyticsConfig) -> None:
        self._config = config

    def record(
        self,
        event: AnalyticsEventDTO,
        *,
        source: str = "frontend",
        api_key: APIKey | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Record one event. Must never raise — ingestion is fire-and-forget."""
        if not self._config.enabled:
            return
        try:
            fields: dict[str, Any] = {
                "type": "analytics",
                "event": event.event,
                "source": source,
                "session_id": event.session_id,
                "route": event.route,
                "path": event.path,
                "client_ts": event.client_ts.isoformat() if event.client_ts else None,
                "server_ts": datetime.now(UTC).isoformat(),
                "user_agent": user_agent,
                "properties": event.properties,
                # The env-registry token IS the credential, so never log it —
                # only whether a valid key was presented and with what scopes.
                "has_api_key": api_key is not None,
                "api_key_scopes": _scopes(api_key),
                "api_key_name": api_key.name if api_key is not None else None,
            }
            logger.info("analytics event: %s", event.event, extra=fields)
        except Exception:
            logger.exception("Failed to record analytics event")


def _scopes(api_key: APIKey | None) -> str | None:
    if api_key is None:
        return None
    return "+".join(s for s, on in (("read", api_key.read), ("write", api_key.write), ("admin", api_key.admin)) if on)
