import time
import uuid
import logging

from fastapi import Security, APIRouter, WebSocket, WebSocketDisconnect

from canopy.core.errors import ErrorCode
from canopy.core.security import ws_require_read_access
from canopy.hare.schemas.ws import PingMessage, WSMessageError, parse_ws_message
from canopy.hare.dependencies import PVBrokerDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["Hare / WebSocket"])


async def _send_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "code": ErrorCode.INVALID_MESSAGE.value, "message": message})


@router.websocket("/pvs", dependencies=[Security(ws_require_read_access)])
async def ws_pvs(websocket: WebSocket, broker: PVBrokerDep) -> None:
    """Live PV stream with diff-based updates.

    Auth: ``X-API-Key`` header or ``?api_key=`` query param (read access).

    Client -> server:
    - ``{"type": "subscribe", "pvNames": ["PV1", "PV2"]}``
    - ``{"type": "unsubscribe", "pvNames": ["PV1"]}``
    - ``{"type": "ping"}``

    Server -> client:
    - ``{"type": "initial", "data": {"PV1": {...PVValue}}, "meta": {"PV1": {...PVMeta}}, "count": N}``
      — sent on subscribe. ``data`` is the dynamic value; ``meta`` is the static
      per-channel metadata (``valueKind``, units, precision, ``enumChoices``,
      display/control/alarm limits) a client reads once to configure a display.
    - ``{"type": "diff", "data": {"PV1": {...PVValue}}, "count": N, "timestamp": T}``
      — only the dynamic ``PVValue`` (value/severity/ts/``valueKind``); metadata is
      not re-sent. ``value`` is a scalar, a string, an enum index, a 1-D waveform
      list, or a ``{labels, columns}`` table per ``valueKind``.
    - ``{"type": "heartbeat", "timestamp": T, "backend": "simulation"}``
    - ``{"type": "pong", "timestamp": T}``
    - ``{"type": "error", "code": "bad_request", "message": "..."}`` (``code`` is an
      `ErrorCode`, matching the REST error envelope's ``code`` field). On a
      ``subscribe`` with malformed names the error carries ``code`` ``invalid_pv_name``
      and a ``pvNames`` list of the rejected names; the batch's valid PVs still
      subscribe. This lets a client tell "rejected at subscribe time" apart from
      "valid PV that hasn't produced a value yet."
    """
    await websocket.accept()
    client_id = uuid.uuid4().hex
    broker.add_client(client_id, websocket)
    try:
        while True:
            # A malformed frame (non-JSON, non-object, wrong field types) sends an
            # error frame and keeps the connection open -- one bad message must
            # not disconnect a client and drop all its subscriptions.
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                await _send_error(websocket, "message must be valid JSON")
                continue

            try:
                message = parse_ws_message(raw)
            except WSMessageError as exc:
                await _send_error(websocket, str(exc))
                continue

            if isinstance(message, PingMessage):
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
            elif message.type == "subscribe":
                await broker.subscribe(client_id, message.pv_names)
            else:  # unsubscribe
                await broker.unsubscribe(client_id, message.pv_names)
    except WebSocketDisconnect:
        await broker.remove_client(client_id)
    except Exception as exc:
        logger.warning("WebSocket error for client %s: %s", client_id, exc)
        await broker.remove_client(client_id)
