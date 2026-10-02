"""Manages live formula subscriptions over WebSocket connections.

Multiple clients subscribing to the same formula share a single eval loop
(SharedEval). Results are fanned out to all listeners. This deduplicates
computation when many clients watch the same formula.

Each SharedEval remembers its latest outcome (a result, or an error) and replays
it to a listener that joins later. Inputs that never change (a supply's on/off
status, a static setpoint) would otherwise leave a late joiner without any
result, because the loop only re-evaluates when an input changes.

While inputs have no data yet the loop keeps retrying server-side. Once that
wait outlasts ``input_wait_notify_s`` the listeners get one error frame with
``"retryable": true``; the next result supersedes it.
"""

from __future__ import annotations

import time
import asyncio
import hashlib
import logging
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from canopy.fox.errors import SubscriptionLimitExceeded
from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.formula import FormulaSpec
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.fox.services.formula_service import FormulaService

_RETRIABLE_CODES = frozenset({ErrorCode.FORMULA_INPUT_MISSING, ErrorCode.PV_NOT_BUFFERED})
# Re-evaluation cadence while an input has no data yet.
_INPUT_RETRY_DELAY_S = 0.25

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
    # Latest outcome fanned out, replayed to late joiners: the frame minus its
    # per-listener ``id``. None until the first result or error is sent.
    last_frame: dict | None = None
    # Monotonic time the current run of retryable failures began, or None.
    waiting_since: float | None = None


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
        *,
        min_interval_ms: int = 50,
        max_subscriptions_per_client: int = 1_000,
        input_wait_notify_s: float = 1.0,
    ) -> None:
        self._formula_service = formula_service
        self._on_change = on_change_fn
        self._subscribe_live = subscribe_live_fn
        self._subscribe_latest = subscribe_latest_fn or subscribe_live_fn
        self._min_interval_ms = min_interval_ms
        self._max_subscriptions_per_client = max_subscriptions_per_client
        self._input_wait_notify_s = input_wait_notify_s
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

        # Floor the poll interval so a client can't request a CPU-pinning loop.
        if 0 < interval_ms < self._min_interval_ms:
            interval_ms = self._min_interval_ms

        # Cap live subscriptions per client (a re-subscribe of an existing id
        # replaces, so it doesn't count against the cap).
        existing = self._client_subs.get(client_id, {})
        if sub_id not in existing and len(existing) >= self._max_subscriptions_per_client:
            raise SubscriptionLimitExceeded(
                f"client has reached the maximum of {self._max_subscriptions_per_client} live subscriptions"
            )

        if client_id not in self._client_subs:
            self._client_subs[client_id] = {}
        elif sub_id in self._client_subs[client_id]:
            await self.remove_subscription(client_id, sub_id)
            if client_id not in self._client_subs:
                self._client_subs[client_id] = {}

        key = _compute_key(spec, t0_ns, interval_ms)
        listener = _Listener(client_id=client_id, sub_id=sub_id, send_fn=send_fn)

        joined: _SharedEval | None = self._shared_evals.get(key)
        if joined is not None:
            joined.listeners.append(listener)
        else:
            shared = _SharedEval(key=key, spec=spec, t0_ns=t0_ns, interval_ms=interval_ms)
            shared.listeners.append(listener)
            # Register only after a successful start: if _start_shared_eval
            # raises partway through, tear down what it set up and propagate,
            # leaving no orphaned (task-less) SharedEval that later subscribers
            # would silently attach to.
            try:
                await self._start_shared_eval(shared)
            except Exception:
                self._teardown_shared(shared)
                raise
            self._shared_evals[key] = shared

        self._client_subs[client_id][sub_id] = key
        logger.debug("Client %s subscribed %s → shared eval %s", client_id, sub_id, key[:8])
        if joined is not None:
            await self._replay_last(joined, listener)

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
        await send_fn({"type": "completed", "id": sub_id})

    async def update_subscription(
        self,
        client_id: str,
        sub_id: str,
        send_fn: SendFn,
        *,
        spec: FormulaSpec | None = None,
        t0_ns: int | None = None,
        interval_ms: int | None = None,
    ) -> bool:
        """Update a subscription's parameters.

        Detaches the listener from its current SharedEval and moves it to a new
        one with the updated params. Other listeners on the old SharedEval are
        unaffected. Returns False only if the subscription doesn't exist.
        """
        subs = self._client_subs.get(client_id)
        if not subs or sub_id not in subs:
            return False

        key = subs[sub_id]
        shared = self._shared_evals.get(key)
        if shared is None:
            return False

        new_spec = spec if spec is not None else shared.spec
        new_t0 = t0_ns if t0_ns is not None else shared.t0_ns
        new_interval = interval_ms if interval_ms is not None else shared.interval_ms

        # Floor the poll interval, same as add_subscription: without this a client
        # could subscribe legitimately then update to interval_ms=1 and spin up a
        # ~1000 eval/sec poll loop that pins a CPU core.
        if 0 < new_interval < self._min_interval_ms:
            new_interval = self._min_interval_ms

        new_key = _compute_key(new_spec, new_t0, new_interval)
        if new_key == key:
            # Params resolved to the same eval (e.g. an update that changed
            # nothing). Detaching-then-reattaching here would needlessly tear
            # down and restart a live eval, so leave it as is.
            return True

        # Start (or join) the new SharedEval *before* touching the old one. If
        # _start_shared_eval raises, the old eval and this listener's mapping
        # are still intact, so the subscription keeps producing data instead of
        # being orphaned on a deleted key.
        listener = _Listener(client_id=client_id, sub_id=sub_id, send_fn=send_fn)
        joined = self._shared_evals.get(new_key)
        if joined is not None:
            joined.listeners.append(listener)
        else:
            new_shared = _SharedEval(key=new_key, spec=new_spec, t0_ns=new_t0, interval_ms=new_interval)
            new_shared.listeners.append(listener)
            try:
                await self._start_shared_eval(new_shared)
            except Exception:
                self._teardown_shared(new_shared)
                raise
            self._shared_evals[new_key] = new_shared

        # New eval is live; now detach from the old one and tear it down if this
        # was its last listener.
        shared.listeners = [ls for ls in shared.listeners if not (ls.client_id == client_id and ls.sub_id == sub_id)]
        if not shared.listeners:
            self._teardown_shared(shared)
            del self._shared_evals[key]

        subs[sub_id] = new_key
        if joined is not None:
            await self._replay_last(joined, listener)
        return True

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

    async def _replay_last(self, shared: _SharedEval, listener: _Listener) -> None:
        """Send a joining listener the eval's latest outcome, if there is one.

        Called right after the listener was appended, with no await in between,
        so a fan-out that runs while this send is in flight queues behind it on
        the client's send lock and the joiner never sees an older frame last.
        """
        if shared.last_frame is None:
            return
        try:
            await listener.send_fn({**shared.last_frame, "id": listener.sub_id})
        except Exception:
            # A dead socket is reaped by the next fan-out or by its disconnect.
            logger.debug("replay to %s/%s failed", listener.client_id, listener.sub_id, exc_info=True)

    async def _broadcast(self, shared: _SharedEval, frame: dict) -> None:
        """Record ``frame`` as the latest outcome and send it to every listener."""
        shared.last_frame = frame
        dead: list[_Listener] = []
        for listener in list(shared.listeners):
            try:
                await listener.send_fn({**frame, "id": listener.sub_id})
            except Exception:
                dead.append(listener)
        if dead:
            shared.listeners = [ls for ls in shared.listeners if ls not in dead]

    async def _fan_out(self, shared: _SharedEval, result_data: dict) -> None:
        shared.waiting_since = None
        await self._broadcast(shared, {"type": "result", "data": result_data})

    async def _fan_out_error(self, shared: _SharedEval, code: str, message: str) -> None:
        shared.waiting_since = None
        await self._broadcast(shared, {"type": "error", "code": code, "message": message})

    async def _note_input_wait(self, shared: _SharedEval, exc: CanopyError) -> None:
        """Handle a retryable failure: the caller retries, the client hears once.

        The first ``input_wait_notify_s`` of a wait stay silent -- on a fresh
        subscribe the inputs normally land within milliseconds. After that the
        listeners get one error frame flagged ``retryable``; it is re-sent only
        if the reason changes, never on every retry.
        """
        now = time.monotonic()
        if shared.waiting_since is None:
            shared.waiting_since = now
        if now - shared.waiting_since < self._input_wait_notify_s:
            return
        frame = {"type": "error", "code": exc.error_code.value, "message": exc.detail, "retryable": True}
        if shared.last_frame == frame:
            return
        await self._broadcast(shared, frame)

    async def _eval_loop(self, shared: _SharedEval) -> None:
        try:
            while True:
                await shared.dirty.wait()
                shared.dirty.clear()
                try:
                    result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
                    await self._fan_out(shared, result.model_dump(by_alias=True))
                except CanopyError as exc:
                    if exc.error_code in _RETRIABLE_CODES:
                        await self._note_input_wait(shared, exc)
                        await asyncio.sleep(_INPUT_RETRY_DELAY_S)
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
                    if exc.error_code in _RETRIABLE_CODES:
                        await self._note_input_wait(shared, exc)
                    else:
                        await self._fan_out_error(shared, exc.error_code.value, exc.detail)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Poll loop error for shared %s: %s", shared.key[:8], exc, exc_info=True)
                    await self._fan_out_error(shared, ErrorCode.UNHANDLED_EXCEPTION.value, str(exc))
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
