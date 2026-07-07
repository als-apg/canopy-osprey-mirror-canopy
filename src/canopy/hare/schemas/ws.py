"""Client -> server WebSocket message schemas for the Hare PV stream.

Raw frames are validated through these before dispatch so a malformed message
(non-object JSON, wrong field types, ``pvNames`` as a bare string) produces a
clean ``error`` frame instead of an ``AttributeError``/``TypeError`` that drops
the whole connection.
"""

from typing import Literal

from pydantic import ValidationError

from canopy.contracts.base import CanopyModel


class WSMessageError(ValueError):
    """A client WebSocket frame failed validation. Carries a client-safe reason."""


class SubscribeMessage(CanopyModel):
    """``{"type": "subscribe" | "unsubscribe", "pvNames": ["PV1", ...]}``.

    ``pvNames`` is a list of strings: a bare string is rejected (not exploded
    into per-character PVs), and non-string elements are rejected.
    """

    type: Literal["subscribe", "unsubscribe"]
    pv_names: list[str] = []


class PingMessage(CanopyModel):
    type: Literal["ping"]


def _summarize(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ())) or "message"
    return f"{loc}: {err.get('msg', 'invalid')}"


def parse_ws_message(raw: object) -> SubscribeMessage | PingMessage:
    """Validate a decoded client frame, or raise :class:`WSMessageError`."""
    if not isinstance(raw, dict):
        raise WSMessageError("message must be a JSON object")
    mtype = raw.get("type")
    try:
        if mtype in ("subscribe", "unsubscribe"):
            return SubscribeMessage.model_validate(raw)
        if mtype == "ping":
            return PingMessage.model_validate(raw)
    except ValidationError as exc:
        raise WSMessageError(_summarize(exc)) from exc
    raise WSMessageError(f"unknown message type: {mtype!r}")
