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
    - ``{"type": "initial", "data": {"PV1": {...}}, "count": N}``
    - ``{"type": "diff", "data": {"PV1": {...}}, "count": N, "timestamp": T}``
    - ``{"type": "heartbeat", "timestamp": T, "backend": "simulation"}``
    - ``{"type": "pong", "timestamp": T}``
    - ``{"type": "error", "code": "bad_request", "message": "..."}`` (``code`` is an
      `ErrorCode`, matching the REST error envelope's ``code`` field)
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
