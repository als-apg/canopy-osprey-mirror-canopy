from typing import Any
from datetime import datetime

from pydantic import Field

from canopy.contracts.base import CanopyModel


class AnalyticsEventDTO(CanopyModel):
    """Incoming usage or error event from a frontend client.

    Wire fields are camelCase (``sessionId``, ``clientTs``) via CanopyModel.
    ``properties`` is free-form; error events conventionally carry
    ``properties.error`` / ``properties.stack``.
    """

    event: str
    session_id: str | None = None
    route: str | None = None
    path: str | None = None
    client_ts: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
