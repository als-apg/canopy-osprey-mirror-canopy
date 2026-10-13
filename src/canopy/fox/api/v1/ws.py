"""WebSocket endpoint for live formula subscriptions.

Clients subscribe to formula specs and receive push-based re-evaluated
results whenever input PVs update via Vole change notifications.
"""

import time
import uuid
import asyncio
import logging

from fastapi import Security, APIRouter, WebSocket, WebSocketDisconnect

from canopy.fox.errors import TABLE_UPDATE_UNSUPPORTED_MESSAGE
from canopy.core.errors import ErrorCode, CanopyError
from canopy.core.security import ws_require_read_access
from canopy.fox.dependencies import SubscriptionManagerDep
from canopy.contracts.formula import FormulaSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["Fox / WebSocket"])


class _WSValidationError(ValueError):
    """A client frame field was the wrong type. Carries a client-safe reason."""


def _coerce_int(value: object, field: str, default: int = 0) -> int:
    """Coerce a JSON numeric to int, or raise. ``None`` yields ``default``.

    Rejects strings/bools/objects up front so a value like ``"fast"`` never
    reaches the subscription manager (where it used to raise mid-setup and
    orphan a shared eval).
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _WSValidationError(f"'{field}' must be a number")
    return int(value)


def _coerce_opt_int(value: object, field: str) -> int | None:
    """Like :func:`_coerce_int` but preserves ``None`` (meaning "unchanged")."""
    return None if value is None else _coerce_int(value, field)


async def _send_error(send_fn, code: str, message: str, *, sub_id: str | None = None, retryable: bool = False) -> None:
    # Errors sent from here are contract or validation rejects (or a failed
    # start): nothing retries them server-side, so ``retryable`` is false.
    frame: dict = {"type": "error", "code": code, "message": message, "retryable": retryable}
    if sub_id is not None:
        frame["id"] = sub_id
    await send_fn(frame)


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
    - ``{"type": "error", "code": "...", "message": "...", "retryable": bool}``
    - ``{"type": "pong", "timestamp": T}``

    A live ``table`` polls at a server-derived interval (``intervalMs`` is
    ignored) and its result frames carry ``data = {kind: "table", table: {...}}``
    with nulls for NaN/±inf. Tables are never updated in place.
    """
    await websocket.accept()
    client_id = uuid.uuid4().hex
    send_lock = asyncio.Lock()

    async def send_fn(msg: dict | str) -> None:
        # Every Fox frame is a WebSocket *text* frame (FR4): a dict goes out via
        # send_json (text mode), a table eval's pre-encoded JSON via send_text.
        async with send_lock:
            if isinstance(msg, str):
                await websocket.send_text(msg)
            else:
                await websocket.send_json(msg)

    try:
        while True:
            # A malformed frame sends an error and keeps the socket open -- one
            # bad message must not drop the connection and all its subscriptions.
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, "message must be valid JSON")
                continue
            if not isinstance(message, dict):
                await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, "message must be a JSON object")
                continue
            mtype = message.get("type")

            if mtype == "subscribe":
                spec_raw = message.get("spec")

                if not spec_raw:
                    await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, "subscribe requires 'spec'")
                    continue

                try:
                    t0_ns = _coerce_int(message.get("t0Ns"), "t0Ns")
                    t1_ns = _coerce_int(message.get("t1Ns"), "t1Ns")
                    interval_ms = _coerce_int(message.get("intervalMs"), "intervalMs")
                except _WSValidationError as exc:
                    await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, str(exc))
                    continue

                try:
                    spec = FormulaSpec(**spec_raw)
                except Exception as exc:
                    await _send_error(send_fn, ErrorCode.INVALID_SPEC.value, f"invalid spec: {exc}")
                    continue

                # A live table is checked whole (lookback, caps, bindings, every
                # column) before it is acknowledged, pinned or subscribed. A
                # table with a fixed t1 falls through to the one-shot reject.
                if spec.kind == "table" and t1_ns == 0:
                    try:
                        manager.validate_table(spec, t0_ns)
                    except CanopyError as exc:
                        await _send_error(send_fn, exc.error_code.value, exc.detail)
                        continue

                sub_id = uuid.uuid4().hex
                await send_fn({"type": "subscribed", "id": sub_id})
                try:
                    await manager.add_subscription(
                        client_id, sub_id, spec, t0_ns, t1_ns, send_fn, interval_ms=interval_ms
                    )
                except CanopyError as exc:
                    await _send_error(send_fn, exc.error_code.value, exc.detail, sub_id=sub_id)
                except Exception as exc:
                    logger.warning("Fox subscribe failed for %s: %s", sub_id, exc)
                    await _send_error(send_fn, ErrorCode.UNHANDLED_EXCEPTION.value, str(exc), sub_id=sub_id)

            elif mtype == "update":
                sub_id = message.get("id", "")
                spec_update = message.get("spec")
                # Tables are never updated in place; reject before any other check.
                if isinstance(spec_update, dict) and spec_update.get("kind") == "table":
                    await _send_error(
                        send_fn,
                        ErrorCode.TABLE_UPDATE_UNSUPPORTED.value,
                        TABLE_UPDATE_UNSUPPORTED_MESSAGE,
                        sub_id=sub_id or None,
                    )
                    continue
                try:
                    t0_update = _coerce_opt_int(message.get("t0Ns"), "t0Ns")
                    interval_update = _coerce_opt_int(message.get("intervalMs"), "intervalMs")
                except _WSValidationError as exc:
                    await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, str(exc))
                    continue

                if not sub_id:
                    await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, "update requires 'id'")
                    continue

                parsed_spec = None
                if spec_update is not None:
                    try:
                        parsed_spec = FormulaSpec(**spec_update)
                    except Exception as exc:
                        await _send_error(send_fn, ErrorCode.INVALID_SPEC.value, f"invalid spec: {exc}", sub_id=sub_id)
                        continue

                # Guard like the subscribe handler: a failure while starting the
                # new eval must send an error frame, not fall through to the outer
                # handler and drop the whole connection (and all its subscriptions).
                try:
                    ok = await manager.update_subscription(
                        client_id, sub_id, send_fn, spec=parsed_spec, t0_ns=t0_update, interval_ms=interval_update
                    )
                except CanopyError as exc:
                    await _send_error(send_fn, exc.error_code.value, exc.detail, sub_id=sub_id)
                    continue
                except Exception as exc:
                    logger.warning("Fox update failed for %s: %s", sub_id, exc)
                    await _send_error(send_fn, ErrorCode.UNHANDLED_EXCEPTION.value, str(exc), sub_id=sub_id)
                    continue

                if ok:
                    await send_fn({"type": "updated", "id": sub_id})
                else:
                    await _send_error(send_fn, ErrorCode.NOT_FOUND.value, "subscription not found", sub_id=sub_id)

            elif mtype == "unsubscribe":
                sub_id = message.get("id", "")
                await manager.remove_subscription(client_id, sub_id)

            elif mtype == "ping":
                await send_fn({"type": "pong", "timestamp": time.time()})

            else:
                await _send_error(send_fn, ErrorCode.INVALID_MESSAGE.value, f"unknown message type: {mtype!r}")
    except WebSocketDisconnect:
        await manager.remove_client(client_id)
    except Exception as exc:
        logger.warning("Fox WS error for client %s: %s", client_id, exc)
        await manager.remove_client(client_id)
