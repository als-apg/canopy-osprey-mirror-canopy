"""Manages live formula subscriptions over WebSocket connections.

Each subscription gets a dirty-flag Event and a background eval loop.
When Vole notifies that an input PV updated, the flag is set; the loop
wakes, re-evaluates, and pushes the result to the client.
"""

from __future__ import annotations

import asyncio
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
class _FormulaSubscription:
    sub_id: str
    spec: FormulaSpec
    t0_ns: int
    t1_ns: int
    interval_ms: int = 0
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    unsub_fns: list[Callable[[], None]] = field(default_factory=list)


class SubscriptionManager:
    """Orchestrates live formula subscriptions for WebSocket clients."""

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
        self._client_subs: dict[str, dict[str, _FormulaSubscription]] = {}

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
        """Register a formula subscription and start its eval loop.

        If t1_ns is non-zero (fixed historical window), evaluates once and
        sends the result without opening a live subscription.

        If interval_ms > 0, uses poll mode (timer-based) instead of push mode
        (Vole change notifications).
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

        sub = _FormulaSubscription(sub_id=sub_id, spec=spec, t0_ns=t0_ns, t1_ns=t1_ns, interval_ms=interval_ms)

        pvs = list(spec.bindings.values())

        if interval_ms > 0:
            sub.task = asyncio.create_task(self._poll_loop(sub, send_fn))
        else:
            canonical_pvs = [strip_protocol_prefix(pv) for pv in pvs]
            for pv in canonical_pvs:
                unsub_fn = self._on_change(pv, self._make_notify_callback(sub))
                sub.unsub_fns.append(unsub_fn)

            if t0_ns == 0:
                await self._subscribe_latest(pvs)
            else:
                await self._subscribe_live(pvs)
            sub.dirty.set()
            sub.task = asyncio.create_task(self._eval_loop(sub, send_fn))

        self._client_subs[client_id][sub_id] = sub
        logger.debug("Client %s subscribed: %s (interval=%dms)", client_id, sub_id, interval_ms)

    async def _one_shot(self, sub_id: str, spec: FormulaSpec, t0_ns: int, t1_ns: int, send_fn: SendFn) -> None:
        """Evaluate a fixed time window once and send the result."""
        try:
            result = await self._formula_service.evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)
            await send_fn(
                {
                    "type": "result",
                    "id": sub_id,
                    "data": result.model_dump(by_alias=True),
                }
            )
        except CanopyError as exc:
            await send_fn(
                {
                    "type": "error",
                    "id": sub_id,
                    "code": exc.error_code.value,
                    "message": exc.detail,
                }
            )
        except Exception as exc:
            await send_fn(
                {
                    "type": "error",
                    "id": sub_id,
                    "code": ErrorCode.UNHANDLED_EXCEPTION.value,
                    "message": str(exc),
                }
            )

    async def remove_subscription(self, client_id: str, sub_id: str) -> None:
        """Cancel one formula subscription."""
        subs = self._client_subs.get(client_id)
        if not subs:
            return
        sub = subs.pop(sub_id, None)
        if sub is None:
            return
        self._teardown_sub(sub)
        if not subs:
            del self._client_subs[client_id]

    async def remove_client(self, client_id: str) -> None:
        """Cancel ALL subscriptions for a disconnecting client."""
        subs = self._client_subs.pop(client_id, None)
        if not subs:
            return
        for sub in subs.values():
            self._teardown_sub(sub)
        logger.debug("Removed all subscriptions for client %s", client_id)

    async def shutdown(self) -> None:
        """Clean shutdown: cancel all tasks, unregister all Vole callbacks."""
        for client_id in list(self._client_subs):
            await self.remove_client(client_id)

    def _teardown_sub(self, sub: _FormulaSubscription) -> None:
        if sub.task is not None:
            sub.task.cancel()
        for unsub_fn in sub.unsub_fns:
            unsub_fn()
        sub.unsub_fns.clear()

    def _make_notify_callback(self, sub: _FormulaSubscription) -> Callable[[str], Awaitable[None]]:
        async def _on_pv_change(pv: str) -> None:
            sub.dirty.set()

        return _on_pv_change

    async def _eval_loop(self, sub: _FormulaSubscription, send_fn: SendFn) -> None:
        _INPUT_RETRY_DELAY = 0.25

        try:
            while True:
                await sub.dirty.wait()
                sub.dirty.clear()
                try:
                    result = await self._formula_service.evaluate(sub.spec, t0_ns=sub.t0_ns, t1_ns=sub.t1_ns)
                    await send_fn(
                        {
                            "type": "result",
                            "id": sub.sub_id,
                            "data": result.model_dump(by_alias=True),
                        }
                    )
                except CanopyError as exc:
                    if exc.error_code in _RETRIABLE_CODES:
                        await asyncio.sleep(_INPUT_RETRY_DELAY)
                        sub.dirty.set()
                        continue
                    await send_fn(
                        {
                            "type": "error",
                            "id": sub.sub_id,
                            "code": exc.error_code.value,
                            "message": exc.detail,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Eval loop error for sub %s: %s", sub.sub_id, exc, exc_info=True)
                    await send_fn(
                        {
                            "type": "error",
                            "id": sub.sub_id,
                            "code": ErrorCode.UNHANDLED_EXCEPTION.value,
                            "message": str(exc),
                        }
                    )
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self, sub: _FormulaSubscription, send_fn: SendFn) -> None:
        interval_s = sub.interval_ms / 1000.0

        try:
            while True:
                try:
                    result = await self._formula_service.evaluate(sub.spec, t0_ns=sub.t0_ns, t1_ns=sub.t1_ns)
                    await send_fn(
                        {
                            "type": "result",
                            "id": sub.sub_id,
                            "data": result.model_dump(by_alias=True),
                        }
                    )
                except CanopyError as exc:
                    if exc.error_code not in _RETRIABLE_CODES:
                        await send_fn(
                            {
                                "type": "error",
                                "id": sub.sub_id,
                                "code": exc.error_code.value,
                                "message": exc.detail,
                            }
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Poll loop error for sub %s: %s", sub.sub_id, exc, exc_info=True)
                    await send_fn(
                        {
                            "type": "error",
                            "id": sub.sub_id,
                            "code": ErrorCode.UNHANDLED_EXCEPTION.value,
                            "message": str(exc),
                        }
                    )
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
