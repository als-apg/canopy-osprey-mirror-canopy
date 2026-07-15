import time
import uuid
import logging

from fastapi import Security, APIRouter, WebSocket, WebSocketDisconnect

from canopy.core.errors import ErrorCode
from canopy.core.security import ws_require_read_access
from canopy.hare.dependencies import PVBrokerDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["Hare / WebSocket"])


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
            message = await websocket.receive_json()
            mtype = message.get("type")
            if mtype == "subscribe":
                await broker.subscribe(client_id, list(message.get("pvNames", [])))
            elif mtype == "unsubscribe":
                await broker.unsubscribe(client_id, list(message.get("pvNames", [])))
            elif mtype == "ping":
                await websocket.send_json({"type": "pong", "timestamp": time.time()})
            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": ErrorCode.BAD_REQUEST.value,
                        "message": f"unknown message type: {mtype!r}",
                    }
                )
    except WebSocketDisconnect:
        await broker.remove_client(client_id)
    except Exception as exc:
        logger.warning("WebSocket error for client %s: %s", client_id, exc)
        await broker.remove_client(client_id)
