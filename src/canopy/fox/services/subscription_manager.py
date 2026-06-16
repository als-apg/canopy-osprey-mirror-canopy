"""Manages live formula subscriptions over WebSocket connections.

Multiple clients subscribing to the same formula share a single eval loop
(SharedEval). Results are fanned out to all listeners. This deduplicates
computation when many clients watch the same formula.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.formula import FormulaSpec
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.fox.services.formula_service import FormulaService

_RETRIABLE_CODES = frozenset({ErrorCode.FORMULA_INPUT_MISSING, ErrorCode.PV_NOT_BUFFERED})

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]
OnChangeFn = Callable[[str, Callable[[str], Awaitable[None]]], Callable[[], None]]
SubscribeFn = Callable[[list[str]], Awaitable[None]]


@dataclass
class _Listener:
    client_id: str
    sub_id: str
    send_fn: SendFn


@dataclass
class _SharedEval:
    key: str
    spec: FormulaSpec
    t0_ns: int
    interval_ms: int
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    unsub_fns: list[Callable[[], None]] = field(default_factory=list)
    listeners: list[_Listener] = field(default_factory=list)


def _compute_key(spec: FormulaSpec, t0_ns: int, interval_ms: int) -> str:
    raw = f"{spec.model_dump_json()}|{t0_ns}|{interval_ms}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SubscriptionManager:
    """Orchestrates live formula subscriptions for WebSocket clients.

    Deduplicates evaluation: subscriptions with identical parameters share
    one eval loop and fan out results to all listeners.
    """

    def __init__(
        self,
        formula_service: FormulaService,
        on_change_fn: OnChangeFn,
        subscribe_live_fn: SubscribeFn,
        subscribe_latest_fn: SubscribeFn | None = None,
    ) -> None:
        self._formula_service = formula_service
        self._on_change = on_change_fn
        self._subscribe_live = subscribe_live_fn
        self._subscribe_latest = subscribe_latest_fn or subscribe_live_fn
        self._shared_evals: dict[str, _SharedEval] = {}
        self._client_subs: dict[str, dict[str, str]] = {}

    async def add_subscription(
        self,
        client_id: str,
        sub_id: str,
        spec: FormulaSpec,
        t0_ns: int,
        t1_ns: int,
        send_fn: SendFn,
        *,
        interval_ms: int = 0,
    ) -> None:
        """Register a formula subscription.

        If t1_ns is non-zero (fixed historical window), evaluates once and
        sends the result without opening a live subscription.

        If interval_ms > 0, uses poll mode (timer-based) instead of push mode.

        Multiple clients with identical parameters share one eval loop.
        """
        if t1_ns != 0:
            await self._one_shot(sub_id, spec, t0_ns, t1_ns, send_fn)
            return

        if client_id not in self._client_subs:
            self._client_subs[client_id] = {}
        elif sub_id in self._client_subs[client_id]:
            await self.remove_subscription(client_id, sub_id)
            if client_id not in self._client_subs:
                self._client_subs[client_id] = {}

        key = _compute_key(spec, t0_ns, interval_ms)
        listener = _Listener(client_id=client_id, sub_id=sub_id, send_fn=send_fn)

        if key in self._shared_evals:
            self._shared_evals[key].listeners.append(listener)
        else:
            shared = _SharedEval(key=key, spec=spec, t0_ns=t0_ns, interval_ms=interval_ms)
            shared.listeners.append(listener)
            self._shared_evals[key] = shared
            await self._start_shared_eval(shared)

        self._client_subs[client_id][sub_id] = key
        logger.debug("Client %s subscribed %s → shared eval %s", client_id, sub_id, key[:8])

    async def _start_shared_eval(self, shared: _SharedEval) -> None:
        pvs = list(shared.spec.bindings.values())

        if shared.interval_ms > 0:
            shared.task = asyncio.create_task(self._poll_loop(shared))
        else:
            canonical_pvs = [strip_protocol_prefix(pv) for pv in pvs]
            for pv in canonical_pvs:
                unsub_fn = self._on_change(pv, self._make_notify_callback(shared))
                shared.unsub_fns.append(unsub_fn)

            if shared.t0_ns == 0:
                await self._subscribe_latest(pvs)
            else:
                await self._subscribe_live(pvs)
            shared.dirty.set()
            shared.task = asyncio.create_task(self._eval_loop(shared))

    async def _one_shot(self, sub_id: str, spec: FormulaSpec, t0_ns: int, t1_ns: int, send_fn: SendFn) -> None:
        try:
            result = await self._formula_service.evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)
            await send_fn({"type": "result", "id": sub_id, "data": result.model_dump(by_alias=True)})
        except CanopyError as exc:
            await send_fn({"type": "error", "id": sub_id, "code": exc.error_code.value, "message": exc.detail})
        except Exception as exc:
            await send_fn(
                {"type": "error", "id": sub_id, "code": ErrorCode.UNHANDLED_EXCEPTION.value, "message": str(exc)}
            )

    async def remove_subscription(self, client_id: str, sub_id: str) -> None:
        subs = self._client_subs.get(client_id)
        if not subs:
            return
        key = subs.pop(sub_id, None)
        if key is None:
            return
        if not subs:
            del self._client_subs[client_id]

        shared = self._shared_evals.get(key)
        if shared is None:
            return
        shared.listeners = [ls for ls in shared.listeners if not (ls.client_id == client_id and ls.sub_id == sub_id)]
        if not shared.listeners:
            self._teardown_shared(shared)
            del self._shared_evals[key]

    async def remove_client(self, client_id: str) -> None:
        subs = self._client_subs.pop(client_id, None)
        if not subs:
            return
        for sub_id, key in subs.items():
            shared = self._shared_evals.get(key)
            if shared is None:
                continue
            shared.listeners = [
                ls for ls in shared.listeners if not (ls.client_id == client_id and ls.sub_id == sub_id)
            ]
            if not shared.listeners:
                self._teardown_shared(shared)
                del self._shared_evals[key]
        logger.debug("Removed all subscriptions for client %s", client_id)

    async def shutdown(self) -> None:
        for shared in list(self._shared_evals.values()):
            self._teardown_shared(shared)
        self._shared_evals.clear()
        self._client_subs.clear()

    def _teardown_shared(self, shared: _SharedEval) -> None:
        if shared.task is not None:
            shared.task.cancel()
        for unsub_fn in shared.unsub_fns:
            unsub_fn()
        shared.unsub_fns.clear()

    def _make_notify_callback(self, shared: _SharedEval) -> Callable[[str], Awaitable[None]]:
        async def _on_pv_change(pv: str) -> None:
            shared.dirty.set()

        return _on_pv_change

    async def _fan_out(self, shared: _SharedEval, result_data: dict) -> None:
        dead: list[_Listener] = []
        for listener in shared.listeners:
            msg = {"type": "result", "id": listener.sub_id, "data": result_data}
            try:
                await listener.send_fn(msg)
            except Exception:
                dead.append(listener)
        if dead:
            shared.listeners = [ls for ls in shared.listeners if ls not in dead]

    async def _fan_out_error(self, shared: _SharedEval, code: str, message: str) -> None:
        for listener in shared.listeners:
            msg = {"type": "error", "id": listener.sub_id, "code": code, "message": message}
            try:
                await listener.send_fn(msg)
            except Exception:
                pass

    async def _eval_loop(self, shared: _SharedEval) -> None:
        _INPUT_RETRY_DELAY = 0.25

        try:
            while True:
                await shared.dirty.wait()
                shared.dirty.clear()
                try:
                    result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
                    await self._fan_out(shared, result.model_dump(by_alias=True))
                except CanopyError as exc:
                    if exc.error_code in _RETRIABLE_CODES:
                        await asyncio.sleep(_INPUT_RETRY_DELAY)
                        shared.dirty.set()
                        continue
                    await self._fan_out_error(shared, exc.error_code.value, exc.detail)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Eval loop error for shared %s: %s", shared.key[:8], exc, exc_info=True)
                    await self._fan_out_error(shared, ErrorCode.UNHANDLED_EXCEPTION.value, str(exc))
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self, shared: _SharedEval) -> None:
        interval_s = shared.interval_ms / 1000.0

        try:
            while True:
                try:
                    result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
                    await self._fan_out(shared, result.model_dump(by_alias=True))
                except CanopyError as exc:
                    if exc.error_code not in _RETRIABLE_CODES:
                        await self._fan_out_error(shared, exc.error_code.value, exc.detail)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Poll loop error for shared %s: %s", shared.key[:8], exc, exc_info=True)
                    await self._fan_out_error(shared, ErrorCode.UNHANDLED_EXCEPTION.value, str(exc))
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
