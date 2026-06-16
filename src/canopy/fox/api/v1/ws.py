"""WebSocket endpoint for live formula subscriptions.

Clients subscribe to formula specs and receive push-based re-evaluated
results whenever input PVs update via Vole change notifications.
"""

import time
import uuid
import logging

from fastapi import Security, APIRouter, WebSocket, WebSocketDisconnect

from canopy.core.errors import ErrorCode
from canopy.core.security import ws_require_read_access
from canopy.fox.dependencies import SubscriptionManagerDep
from canopy.contracts.formula import FormulaSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["Fox / WebSocket"])


@router.websocket("/formulas", dependencies=[Security(ws_require_read_access)])
async def ws_formulas(websocket: WebSocket, manager: SubscriptionManagerDep) -> None:
    """Live formula evaluation stream.

    Client -> server:
    - ``{"type": "subscribe", "spec": {...}, "t0Ns": ..., "t1Ns": ...}``
    - ``{"type": "unsubscribe", "id": "<server-assigned>"}``
    - ``{"type": "ping"}``

    Server -> client:
    - ``{"type": "subscribed", "id": "<assigned>"}``
    - ``{"type": "result", "id": "...", "data": {...}}``
    - ``{"type": "error", "code": "...", "message": "..."}``
    - ``{"type": "pong", "timestamp": T}``
    """
    await websocket.accept()
    client_id = uuid.uuid4().hex

    async def send_fn(msg: dict) -> None:
        await websocket.send_json(msg)

    try:
        while True:
            message = await websocket.receive_json()
            mtype = message.get("type")

            if mtype == "subscribe":
                spec_raw = message.get("spec")
                t0_ns = message.get("t0Ns", 0)
                t1_ns = message.get("t1Ns", 0)
                interval_ms = message.get("intervalMs", 0)

                if not spec_raw:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": ErrorCode.BAD_REQUEST.value,
                            "message": "subscribe requires 'spec'",
                        }
                    )
                    continue

                try:
                    spec = FormulaSpec(**spec_raw)
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": ErrorCode.BAD_REQUEST.value,
                            "message": f"invalid spec: {exc}",
                        }
                    )
                    continue

                sub_id = uuid.uuid4().hex
                await websocket.send_json({"type": "subscribed", "id": sub_id})
                await manager.add_subscription(client_id, sub_id, spec, t0_ns, t1_ns, send_fn, interval_ms=interval_ms)

            elif mtype == "unsubscribe":
                sub_id = message.get("id", "")
                await manager.remove_subscription(client_id, sub_id)

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
        await manager.remove_client(client_id)
    except Exception as exc:
        logger.warning("Fox WS error for client %s: %s", client_id, exc)
        await manager.remove_client(client_id)
