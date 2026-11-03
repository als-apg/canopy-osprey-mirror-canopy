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
from canopy.formula_engine.errors import SubscriptionLimitExceeded
from canopy.formula_engine.services.formula_service import FormulaService

_RETRIABLE_CODES = frozenset({ErrorCode.FORMULA_INPUT_MISSING, ErrorCode.PV_NOT_BUFFERED})
_INPUT_RETRY_DELAY_S = 0.25

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]
OnChangeFn = Callable[[str, Callable[[str], Awaitable[None]]], Callable[[], None]]
SubscribeFn = Callable[[list[str]], Awaitable[None]]


@dataclass(eq=False)
class _Listener:
    """One client subscription attached to a shared evaluation.

    Listeners compare by identity, so a re-subscribe that reuses a client's
    ``sub_id`` creates a listener distinct from the one it replaced.

    Attributes
    ----------
    client_id : str
        WebSocket client that owns the subscription.
    sub_id : str
        Client-chosen subscription id echoed in every message frame.
    send_fn : SendFn
        Coroutine that delivers a message frame to the client.
    delivered : bool
        ``True`` once any result or error frame has reached this listener.
    lock : asyncio.Lock
        Held for every frame sent to this listener, so the attachment and
        ``delivered`` checks and the send happen as one step.
    """

    client_id: str
    sub_id: str
    send_fn: SendFn
    delivered: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class _SharedEval:
    """One evaluation loop shared by every subscription with identical parameters.

    Attributes
    ----------
    key : str
        Deduplication key derived from ``spec``, ``t0_ns`` and ``interval_ms``.
    spec : FormulaSpec
        Formula to evaluate.
    t0_ns : int
        Window start passed to the formula service; ``0`` selects the latest
        value and a negative value is relative to the evaluation time.
    interval_ms : int
        Poll interval in milliseconds; ``0`` selects push mode, driven by PV
        change notifications.
    dirty : asyncio.Event
        Set when an input PV changes; wakes the push-mode eval loop.
    task : asyncio.Task or None
        The running eval or poll loop.
    unsub_fns : list of Callable[[], None]
        Unsubscribe callbacks for the PV change notifications.
    listeners : list of _Listener
        Subscriptions that receive every fanned-out frame.
    catch_up_tasks : set of asyncio.Task
        In-flight late-joiner catch-up evaluations, cancelled on teardown.
    has_output : bool
        ``True`` once the eval has fanned out any frame. `_fan_out` sets it
        before snapshotting the listeners, so a joiner either is in that
        snapshot or sees ``True`` and is given a catch-up.
    """

    key: str
    spec: FormulaSpec
    t0_ns: int
    interval_ms: int
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    unsub_fns: list[Callable[[], None]] = field(default_factory=list)
    listeners: list[_Listener] = field(default_factory=list)
    catch_up_tasks: set[asyncio.Task] = field(default_factory=set)
    has_output: bool = False


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
    ) -> None:
        self._formula_service = formula_service
        self._on_change = on_change_fn
        self._subscribe_live = subscribe_live_fn
        self._subscribe_latest = subscribe_latest_fn or subscribe_live_fn
        self._min_interval_ms = min_interval_ms
        self._max_subscriptions_per_client = max_subscriptions_per_client
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

        If ``t1_ns`` is non-zero (fixed historical window), evaluates once and
        sends the result without opening a live subscription. If
        ``interval_ms > 0``, uses poll mode (timer-based) instead of push mode.

        Multiple clients with identical parameters share one eval loop. A
        subscription that joins an already-running push-mode loop is caught up
        on the current value by `_join_shared`.

        Parameters
        ----------
        client_id : str
            WebSocket client that owns the subscription.
        sub_id : str
            Client-chosen subscription id; re-using a live id replaces it.
        spec : FormulaSpec
            Formula to evaluate.
        t0_ns : int
            Window start; ``0`` selects the latest value and a negative value
            is relative to the evaluation time.
        t1_ns : int
            Window end; non-zero requests a single historical evaluation.
        send_fn : SendFn
            Coroutine that delivers message frames to the client.
        interval_ms : int, optional
            Poll interval in milliseconds, floored to ``min_interval_ms``;
            ``0`` (default) selects push mode.

        Raises
        ------
        SubscriptionLimitExceeded
            If the client already holds ``max_subscriptions_per_client`` live
            subscriptions and ``sub_id`` is new.
        Exception
            Any error raised while starting a new shared eval, after the
            partially started eval is torn down.
        """
        if t1_ns != 0:
            await self._one_shot(sub_id, spec, t0_ns, t1_ns, send_fn)
            return

        # Floor the poll interval so a client can't request a CPU-pinning loop.
        if 0 < interval_ms < self._min_interval_ms:
            interval_ms = self._min_interval_ms

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

        if key in self._shared_evals:
            self._join_shared(self._shared_evals[key], listener)
        else:
            shared = _SharedEval(key=key, spec=spec, t0_ns=t0_ns, interval_ms=interval_ms)
            shared.listeners.append(listener)
            try:
                await self._start_shared_eval(shared)
            except Exception:
                self._teardown_shared(shared)
                raise
            self._shared_evals[key] = shared

        self._client_subs[client_id][sub_id] = key
        logger.debug("Client %s subscribed %s → shared eval %s", client_id, sub_id, key[:8])

    def _join_shared(self, shared: _SharedEval, listener: _Listener) -> None:
        """Attach a listener to an already-running shared evaluation.

        Once a push-mode loop has fanned out, its output has gone to the
        earlier listeners and a static input may never fire another change, so
        the joiner is given its own catch-up evaluation (`_send_catch_up`) in a
        background task. The catch-up targets only the joiner, so listeners
        that already hold the current result receive no duplicate. The task is
        tracked on ``shared.catch_up_tasks`` so `_teardown_shared` cancels it
        together with the loop. A loop with no output yet, and any poll-mode
        loop, needs no catch-up because its next fan-out reaches every
        listener.

        Parameters
        ----------
        shared : _SharedEval
            Running shared evaluation to join.
        listener : _Listener
            Listener to attach.
        """
        shared.listeners.append(listener)
        if shared.interval_ms == 0 and shared.has_output:
            task = asyncio.create_task(self._send_catch_up(shared, listener))
            shared.catch_up_tasks.add(task)
            task.add_done_callback(shared.catch_up_tasks.discard)

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

        Detaches the listener from its current SharedEval and moves it to the
        one matching the updated params, starting it if none is running or
        joining it through `_join_shared` otherwise. Other listeners on the old
        SharedEval are unaffected. An update that resolves to the current
        SharedEval leaves the subscription as it is.

        Parameters
        ----------
        client_id : str
            WebSocket client that owns the subscription.
        sub_id : str
            Subscription id to update.
        send_fn : SendFn
            Coroutine that delivers message frames to the client.
        spec : FormulaSpec, optional
            Replacement formula; ``None`` keeps the current one.
        t0_ns : int, optional
            Replacement window start; ``None`` keeps the current one.
        interval_ms : int, optional
            Replacement poll interval, floored to ``min_interval_ms``; ``None``
            keeps the current one.

        Returns
        -------
        bool
            ``False`` if the subscription does not exist, otherwise ``True``.

        Raises
        ------
        Exception
            Any error raised while starting a new shared eval; the old eval
            and the subscription's mapping are left intact.
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

        if 0 < new_interval < self._min_interval_ms:
            new_interval = self._min_interval_ms

        new_key = _compute_key(new_spec, new_t0, new_interval)
        if new_key == key:
            return True

        listener = _Listener(client_id=client_id, sub_id=sub_id, send_fn=send_fn)
        if new_key in self._shared_evals:
            self._join_shared(self._shared_evals[new_key], listener)
        else:
            new_shared = _SharedEval(key=new_key, spec=new_spec, t0_ns=new_t0, interval_ms=new_interval)
            new_shared.listeners.append(listener)
            try:
                await self._start_shared_eval(new_shared)
            except Exception:
                self._teardown_shared(new_shared)
                raise
            self._shared_evals[new_key] = new_shared

        shared.listeners = [ls for ls in shared.listeners if not (ls.client_id == client_id and ls.sub_id == sub_id)]
        if not shared.listeners:
            self._teardown_shared(shared)
            del self._shared_evals[key]

        subs[sub_id] = new_key
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
        """Stop a shared evaluation's tasks and release its PV change notifications.

        Cancels the eval or poll loop and every in-flight catch-up task, then
        calls each unsubscribe callback. Removing ``shared`` from the
        deduplication map is left to the caller.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation to stop.
        """
        if shared.task is not None:
            shared.task.cancel()
        for task in list(shared.catch_up_tasks):
            task.cancel()
        shared.catch_up_tasks.clear()
        for unsub_fn in shared.unsub_fns:
            unsub_fn()
        shared.unsub_fns.clear()

    def _make_notify_callback(self, shared: _SharedEval) -> Callable[[str], Awaitable[None]]:
        async def _on_pv_change(pv: str) -> None:
            shared.dirty.set()

        return _on_pv_change

    async def _fan_out(self, shared: _SharedEval, frame: dict) -> None:
        """Send a frame to every listener attached to a shared evaluation.

        ``shared.has_output`` is set before the listeners are snapshotted, so a
        listener that joins while the frame is being sent is given a catch-up
        by `_join_shared` instead.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation whose listeners receive the frame.
        frame : dict
            Result or error frame without an ``id``; `_deliver` adds each
            listener's ``sub_id``.
        """
        shared.has_output = True
        for listener in list(shared.listeners):
            await self._deliver(shared, listener, frame)

    async def _deliver(
        self, shared: _SharedEval, listener: _Listener, frame: dict, *, unless_delivered: bool = False
    ) -> None:
        """Send a frame to one listener while it is attached, dropping it if the send fails.

        The attachment check, the optional ``delivered`` check and the send run
        under ``listener.lock``, so a listener detached, or already served,
        while an earlier frame to it was in flight receives nothing from this
        call.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation the listener belongs to.
        listener : _Listener
            Recipient of the frame.
        frame : dict
            Result or error frame without an ``id``; the listener's ``sub_id``
            is added.
        unless_delivered : bool, optional
            Skip the send when the listener has already received a frame.
        """
        async with listener.lock:
            if listener not in shared.listeners or (unless_delivered and listener.delivered):
                return
            try:
                await listener.send_fn({**frame, "id": listener.sub_id})
            except Exception:
                self._drop_listener(shared, listener)
                return
            listener.delivered = True

    def _drop_listener(self, shared: _SharedEval, listener: _Listener) -> None:
        """Remove a single listener and retire the shared eval if it was the last one left.

        The client's subscription mapping is cleared only while it still points
        at ``shared``, so a subscription already moved elsewhere is kept.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation the listener belongs to.
        listener : _Listener
            Listener to remove, matched by identity.
        """
        shared.listeners = [ls for ls in shared.listeners if ls is not listener]
        subs = self._client_subs.get(listener.client_id)
        if subs and subs.get(listener.sub_id) == shared.key:
            subs.pop(listener.sub_id, None)
            if not subs:
                self._client_subs.pop(listener.client_id, None)
        if not shared.listeners and self._shared_evals.get(shared.key) is shared:
            self._teardown_shared(shared)
            del self._shared_evals[shared.key]

    async def _send_catch_up(self, shared: _SharedEval, listener: _Listener) -> None:
        """Evaluate once for a late joiner and send the outcome to it alone.

        A retriable input error is handed back to the shared eval by setting
        ``shared.dirty``, so the eval's own loop does the retrying and fans the
        value out once the inputs arrive. That keeps one retry loop per shared
        eval rather than one per joiner, at the cost of re-sending that value
        to listeners that already held an earlier one. Any other error becomes
        an error frame for the joiner only. The frame goes through `_deliver`
        with ``unless_delivered``, so it is discarded if the shared eval
        reached the joiner first, including a fan-out frame still being sent
        when the catch-up finishes.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation the listener joined.
        listener : _Listener
            Late joiner to catch up.
        """
        if listener not in shared.listeners or listener.delivered:
            return
        try:
            result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
        except CanopyError as exc:
            if exc.error_code in _RETRIABLE_CODES:
                if listener in shared.listeners and not listener.delivered:
                    shared.dirty.set()
                return
            frame = {"type": "error", "code": exc.error_code.value, "message": exc.detail}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Catch-up eval error for shared %s: %s", shared.key[:8], exc, exc_info=True)
            frame = {"type": "error", "code": ErrorCode.UNHANDLED_EXCEPTION.value, "message": str(exc)}
        else:
            frame = {"type": "result", "data": result.model_dump(by_alias=True)}
        await self._deliver(shared, listener, frame, unless_delivered=True)

    async def _eval_loop(self, shared: _SharedEval) -> None:
        """Evaluate whenever an input PV changes and fan out the outcome (push mode).

        Each wake-up consumes ``shared.dirty``; bursts of changes that arrive
        during an evaluation coalesce into one further evaluation. Retriable
        input errors re-set ``dirty`` after ``_INPUT_RETRY_DELAY_S``, other
        errors are fanned out as error frames, and cancellation ends the loop
        quietly.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation to drive.
        """
        try:
            while True:
                await shared.dirty.wait()
                shared.dirty.clear()
                try:
                    result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
                    await self._fan_out(shared, {"type": "result", "data": result.model_dump(by_alias=True)})
                except CanopyError as exc:
                    if exc.error_code in _RETRIABLE_CODES:
                        await asyncio.sleep(_INPUT_RETRY_DELAY_S)
                        shared.dirty.set()
                        continue
                    await self._fan_out(shared, {"type": "error", "code": exc.error_code.value, "message": exc.detail})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Eval loop error for shared %s: %s", shared.key[:8], exc, exc_info=True)
                    await self._fan_out(
                        shared, {"type": "error", "code": ErrorCode.UNHANDLED_EXCEPTION.value, "message": str(exc)}
                    )
        except asyncio.CancelledError:
            pass

    async def _poll_loop(self, shared: _SharedEval) -> None:
        """Evaluate every ``interval_ms`` and fan out the outcome (poll mode).

        Retriable input errors are skipped until the next tick, other errors
        are fanned out as error frames, and cancellation ends the loop quietly.

        Parameters
        ----------
        shared : _SharedEval
            Shared evaluation to drive.
        """
        interval_s = shared.interval_ms / 1000.0

        try:
            while True:
                try:
                    result = await self._formula_service.evaluate(shared.spec, t0_ns=shared.t0_ns, t1_ns=0)
                    await self._fan_out(shared, {"type": "result", "data": result.model_dump(by_alias=True)})
                except CanopyError as exc:
                    if exc.error_code not in _RETRIABLE_CODES:
                        await self._fan_out(
                            shared, {"type": "error", "code": exc.error_code.value, "message": exc.detail}
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Poll loop error for shared %s: %s", shared.key[:8], exc, exc_info=True)
                    await self._fan_out(
                        shared, {"type": "error", "code": ErrorCode.UNHANDLED_EXCEPTION.value, "message": str(exc)}
                    )
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            pass
