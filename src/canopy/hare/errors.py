"""Hare domain exceptions.

Connector-level errors (connection failures, read timeouts) live in
``canopy.epics.base`` and propagate through the broker. These are Hare's own
broker/WebSocket-level errors. Services raise these; the per-module exception
handler maps them to ``{"code": ..., "detail": ...}``.
"""

from canopy.core.errors import CanopyError


class HareError(CanopyError):
    """Base for all Hare broker errors."""
