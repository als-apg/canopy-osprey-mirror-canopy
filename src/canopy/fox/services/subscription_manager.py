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

A live ``table`` eval (FR2-FR4) runs its own loop over a per-eval row store
(``table_store.TableStore``): it polls at a server-derived interval, emits only
when the rows changed, encodes each frame once with orjson off the event loop
and hands every listener the newest frame through its own latest-frame slot, so
a slow socket never delays another. An ended table's store lingers 60 s so a
re-subscribe (a role edit, a return from a long hide) copies its rows instead of
fetching them again.
"""

from __future__ import annotations

import time
import asyncio
import hashlib
import logging
import itertools
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

import numpy as np
import orjson

from canopy.fox.ports import (
    PinFn,
    UnpinFn,
    RingSlice,
    RingSliceFn,
    WatermarkFn,
    SamplesSinceFn,
    EventFaithfulFn,
    EnsureCapacityFn,
    LastAtOrBeforeFn,
    FetchLastBeforeFn,
    FetchWindowBudgetedFn,
)
from canopy.fox.errors import (
    TABLE_UPDATE_UNSUPPORTED_MESSAGE,
    TableLimit,
    FormulaInputOutage,
    TableUpdateUnsupported,
    SubscriptionLimitExceeded,
    is_retryable,
)
from canopy.core.errors import ErrorCode, CanopyError
from canopy.contracts.formula import FormulaSpec
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.fox.services.table_store import (
    TablePort,
    TableView,
    TableStore,
    TableSettings,
    build_view,
    encode_table_data,
)
from canopy.fox.services.formula_service import FormulaService

_RETRIABLE_CODES = frozenset({ErrorCode.FORMULA_INPUT_MISSING, ErrorCode.PV_NOT_BUFFERED})
# Re-evaluation cadence while an input has no data yet.
_INPUT_RETRY_DELAY_S = 0.25

logger = logging.getLogger(__name__)

# A frame dict, or a pre-encoded JSON text frame (table evals). Both go out as
# WebSocket text frames.
SendFn = Callable[[Any], Awaitable[None]]
OnChangeFn = Callable[[str, Callable[[str], Awaitable[None]]], Callable[[], None]]
SubscribeFn = Callable[..., Awaitable[None]]  # (pvs, *, wait_seed=...) where supported


@dataclass(frozen=True)
class _EncodedFrame:
    """A table eval's frame, encoded once per broadcast; each listener's id is spliced in.

    ``head`` is the frame minus its ``id`` (minus ``data`` for a result) and is what
    a repeated wait frame is deduplicated on; ``rest`` is the JSON after the id.
    """

    head: dict
    rest: str

    def for_id(self, sub_id: str) -> str:
        return f'{{"type":{orjson.dumps(self.head["type"]).decode()},"id":{orjson.dumps(sub_id).decode()},{self.rest}}}'


def _encode_head(head: dict) -> _EncodedFrame:
    body = orjson.dumps({k: v for k, v in head.items() if k != "type"}).decode()
    return _EncodedFrame(head=head, rest=body[1:-1])


@dataclass
class _Listener:
    client_id: str
    sub_id: str
    send_fn: SendFn
    # Table evals only: the newest frame not yet sent, drained by ``sender``.
    pending: _EncodedFrame | None = None
    wake: asyncio.Event | None = None
    sender: asyncio.Task | None = None


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
    # Vole pin holder of a scalar/timeseries eval.
    holder: str | None = None
    # Table evals: the resolved spec, the row store and the store version last sent.
    view: TableView | None = None
    store: TableStore | None = None
    sent_version: int = -1
    ingest_ready: asyncio.Event | None = None


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
        pin_fn: PinFn | None = None,
        unpin_fn: UnpinFn | None = None,
        watermark_fn: WatermarkFn | None = None,
        ensure_capacity_fn: EnsureCapacityFn | None = None,
        last_at_or_before_fn: LastAtOrBeforeFn | None = None,
        samples_since_fn: SamplesSinceFn | None = None,
        ring_slice_fn: RingSliceFn | None = None,
        fetch_window_budgeted_fn: FetchWindowBudgetedFn | None = None,
        fetch_last_before_fn: FetchLastBeforeFn | None = None,
        event_faithful_fn: EventFaithfulFn | None = None,
        table_settings: TableSettings | None = None,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._formula_service = formula_service
        self._on_change = on_change_fn
        self._subscribe_live = subscribe_live_fn
        self._subscribe_latest = subscribe_latest_fn or subscribe_live_fn
        # Row-store port (plan task 1.2), consumed by the table eval wired in 1.3.
        self._pin = pin_fn
        self._unpin = unpin_fn
        self._watermark = watermark_fn
        self._ensure_capacity = ensure_capacity_fn
        self._last_at_or_before = last_at_or_before_fn
        self._samples_since = samples_since_fn
        self._ring_slice = ring_slice_fn
        self._fetch_window_budgeted = fetch_window_budgeted_fn
        self._fetch_last_before = fetch_last_before_fn
        self._event_faithful = event_faithful_fn
        self._min_interval_ms = min_interval_ms
        self._max_subscriptions_per_client = max_subscriptions_per_client
        self._input_wait_notify_s = input_wait_notify_s
        self._shared_evals: dict[str, _SharedEval] = {}
        self._client_subs: dict[str, dict[str, str]] = {}
        self._clock_ns = clock_ns or time.time_ns
        self._table = table_settings or _table_settings(formula_service)
        # Row stores of ended tables, oldest first: store id -> (store, since_ns).
        self._lingering: dict[int, tuple[TableStore, int]] = {}
        self._holder_ids = itertools.count(1)
        self._background: set[asyncio.Task] = set()
        self._closing = False

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

        A live table is validated (lookback, caps, bindings, every column)
        before anything is subscribed, so a rejected table never pins a PV or
        reaches Hare. A table with a fixed t1 goes the one-shot way and is
        answered ``table_live_only``.
        """
        if spec.kind == "table" and t1_ns == 0:
            self.validate_table(spec, t0_ns)

        if t1_ns != 0:
            await self._one_shot(sub_id, spec, t0_ns, t1_ns, send_fn)
            return

        # A table polls at a server-derived interval, whatever the client sent.
        interval_ms = self._effective_interval(spec, interval_ms)
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
        if spec.kind == "table":
            tables = sum(
                1
                for sid, k in existing.items()
                if sid != sub_id and (sh := self._shared_evals.get(k)) is not None and sh.spec.kind == "table"
            )
            if tables >= self._table.max_per_client:
                raise TableLimit(f"a client holds at most {self._table.max_per_client} live table subscriptions")

        if client_id not in self._client_subs:
            self._client_subs[client_id] = {}
        elif sub_id in self._client_subs[client_id]:
            await self.remove_subscription(client_id, sub_id)
            if client_id not in self._client_subs:
                self._client_subs[client_id] = {}

        key = _compute_key(spec, t0_ns, interval_ms)

        joined: _SharedEval | None = self._shared_evals.get(key)
        if joined is not None:
            listener = self._new_listener(joined, client_id, sub_id, send_fn)
            joined.listeners.append(listener)
        else:
            shared = _SharedEval(key=key, spec=spec, t0_ns=t0_ns, interval_ms=interval_ms)
            listener = self._new_listener(shared, client_id, sub_id, send_fn)
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

    def validate_table(self, spec: FormulaSpec, t0_ns: int) -> None:
        """Synchronous pre-subscribe check of a live table; raises a ``CanopyError``.

        The WS endpoint calls this before it acknowledges ``subscribed``.
        """
        self._formula_service.check_table_window(t0_ns)
        self._formula_service.check_table(spec)

    def _effective_interval(self, spec: FormulaSpec, interval_ms: int) -> int:
        """A table's poll period: 500 ms (Trigger PV) or max(Δt, 500 ms) (Timer) -- never the client's."""
        if spec.kind != "table":
            return interval_ms
        poll = self._table.poll_ms
        if spec.index == "grid" and spec.grid_dt_ns:
            return max(spec.grid_dt_ns // 1_000_000, poll)
        return poll

    async def _start_shared_eval(self, shared: _SharedEval) -> None:
        pvs = list(shared.spec.bindings.values())

        if shared.spec.kind == "table":
            self._start_table(shared)
            return

        # Keep every input's ring and live ingest alive for the whole eval.
        if self._pin is not None:
            shared.holder = f"fox-eval-{next(self._holder_ids)}"
            self._pin(list(dict.fromkeys(strip_protocol_prefix(pv) for pv in pvs)), shared.holder)

        if shared.interval_ms > 0:
            shared.task = asyncio.create_task(self._poll_loop(shared))
        else:
            canonical_pvs = [strip_protocol_prefix(pv) for pv in pvs]
            for pv in canonical_pvs:
                unsub_fn = self._on_change(pv, self._make_notify_callback(shared))
                shared.unsub_fns.append(unsub_fn)

            # No seed wait on either branch: the WS read loop awaits this inline, so a
            # dead input would hold the client's socket for the connector's read
            # timeout. The eval loop reports a missing input instead.
            subscribe = self._subscribe_latest if shared.t0_ns == 0 else self._subscribe_live
            await self._subscribe_without_seed_wait(subscribe, pvs)
            shared.dirty.set()
            shared.task = asyncio.create_task(self._eval_loop(shared))

    async def _one_shot(self, sub_id: str, spec: FormulaSpec, t0_ns: int, t1_ns: int, send_fn: SendFn) -> None:
        try:
            result = await self._formula_service.evaluate(spec, t0_ns=t0_ns, t1_ns=t1_ns)
            await send_fn({"type": "result", "id": sub_id, "data": result.model_dump(by_alias=True)})
        except CanopyError as exc:
            await send_fn(
                {"type": "error", "id": sub_id, "code": exc.error_code.value, "message": exc.detail, "retryable": False}
            )
        except Exception as exc:
            # A one-shot is over once answered (``completed`` follows): nothing retries it.
            await send_fn(
                {
                    "type": "error",
                    "id": sub_id,
                    "code": ErrorCode.UNHANDLED_EXCEPTION.value,
                    "message": str(exc),
                    "retryable": False,
                }
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

        A table is never updated in place, in either direction: the client
        subscribes the new table and unsubscribes the old one. That reject
        comes before any window or lookback handling.
        """
        if spec is not None and spec.kind == "table":
            raise TableUpdateUnsupported(TABLE_UPDATE_UNSUPPORTED_MESSAGE)

        subs = self._client_subs.get(client_id)
        if not subs or sub_id not in subs:
            return False

        key = subs[sub_id]
        shared = self._shared_evals.get(key)
        if shared is None:
            return False
        if shared.spec.kind == "table":
            raise TableUpdateUnsupported(TABLE_UPDATE_UNSUPPORTED_MESSAGE)

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
        self._detach(shared, client_id, sub_id)

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
        self._detach(shared, client_id, sub_id)

    async def remove_client(self, client_id: str) -> None:
        subs = self._client_subs.pop(client_id, None)
        if not subs:
            return
        for sub_id, key in subs.items():
            shared = self._shared_evals.get(key)
            if shared is None:
                continue
            self._detach(shared, client_id, sub_id)
        logger.debug("Removed all subscriptions for client %s", client_id)

    async def shutdown(self) -> None:
        self._closing = True
        for shared in list(self._shared_evals.values()):
            self._teardown_shared(shared)
        self._shared_evals.clear()
        self._client_subs.clear()
        for store, _ in list(self._lingering.values()):
            self._drop_store(store)
        self._lingering.clear()
        pending = [t for t in self._background if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)

    def _teardown_shared(self, shared: _SharedEval) -> None:
        if shared.task is not None:
            shared.task.cancel()
        for unsub_fn in shared.unsub_fns:
            unsub_fn()
        shared.unsub_fns.clear()
        for listener in shared.listeners:
            self._stop_listener(listener)
        if shared.ingest_ready is not None:
            shared.ingest_ready.set()
        if shared.store is not None:
            store, shared.store = shared.store, None
            if self._closing:
                self._drop_store(store)
            else:
                self._linger(store)
        if shared.holder is not None:
            self._spawn_unpin(shared.holder)
            shared.holder = None

    # --- listeners --------------------------------------------------------- #

    def _new_listener(self, shared: _SharedEval, client_id: str, sub_id: str, send_fn: SendFn) -> _Listener:
        listener = _Listener(client_id=client_id, sub_id=sub_id, send_fn=send_fn)
        if shared.spec.kind == "table":
            listener.wake = asyncio.Event()
            listener.sender = asyncio.create_task(self._drain(shared, listener))
        return listener

    def _detach(self, shared: _SharedEval, client_id: str, sub_id: str) -> None:
        """Remove the listener; the eval's last listener tears it down and unregisters it."""
        kept: list[_Listener] = []
        for ls in shared.listeners:
            if ls.client_id == client_id and ls.sub_id == sub_id:
                self._stop_listener(ls)
            else:
                kept.append(ls)
        shared.listeners = kept
        if not kept:
            self._teardown_shared(shared)
            del self._shared_evals[shared.key]

    @staticmethod
    def _stop_listener(listener: _Listener) -> None:
        if listener.sender is not None:
            listener.sender.cancel()
            listener.sender = None

    async def _drain(self, shared: _SharedEval, listener: _Listener) -> None:
        """Send a table listener its newest frame; intermediate frames of a slow socket are dropped."""
        try:
            while True:
                await listener.wake.wait()
                listener.wake.clear()
                frame, listener.pending = listener.pending, None
                if frame is None:
                    continue
                try:
                    await listener.send_fn(frame.for_id(listener.sub_id))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("table send to %s/%s failed", listener.client_id, listener.sub_id, exc_info=True)
                    shared.listeners = [ls for ls in shared.listeners if ls is not listener]
                    return
        except asyncio.CancelledError:
            pass

    def _post(self, shared: _SharedEval, frame: _EncodedFrame) -> None:
        """Record ``frame`` as the latest outcome and place it in every listener's slot."""
        shared.last_frame = frame
        for listener in shared.listeners:
            listener.pending = frame
            if listener.wake is not None:
                listener.wake.set()

    # --- tables -------------------------------------------------------------- #

    def _port(self) -> TablePort:
        return TablePort(
            last_at_or_before=self._last_at_or_before or _no_record,
            samples_since=self._samples_since or _no_records,
            ring_slice=self._ring_slice or _empty_slice,
            now_ns=self._clock_ns,
            watermark=self._watermark,
            fetch_window_budgeted=self._fetch_window_budgeted,
            fetch_last_before=self._fetch_last_before,
            event_faithful=self._event_faithful,
        )

    def _start_table(self, shared: _SharedEval) -> None:
        """Synchronous part of a table start: claim or create its store, pin, start the loop."""
        spec = shared.spec
        view = build_view(spec, self._formula_service.parse_table_columns(spec), -shared.t0_ns)
        shared.view = view
        shared.ingest_ready = asyncio.Event()
        shared.store = self._claim_store(view, shared.ingest_ready)
        if self._pin is not None:
            holder = f"fox-table-{next(self._holder_ids)}"
            self._pin(list(view.pvs), holder)
            previous, shared.store.holder = shared.store.holder, holder
            if previous is not None:
                self._spawn_unpin(previous)
        shared.task = asyncio.create_task(self._table_loop(shared))

    def _claim_store(self, view: TableView, gate: asyncio.Event) -> TableStore:
        """Move a lingering store of the same index, copy a live one, or start empty (b3, b4:CF-6)."""
        self._sweep_lingering()
        wanted = set(view.pvs)
        candidates: list[tuple[TableStore, bool]] = [
            (sh.store, False)
            for sh in self._shared_evals.values()
            if sh.store is not None and sh.store.index_key == view.index_key
        ]
        candidates += [(st, True) for st, _ in self._lingering.values() if st.index_key == view.index_key]
        if not candidates:
            store = TableStore(view, self._table, self._port())
            store.adopt(view, gate=gate)
            return store
        best, lingering = max(candidates, key=lambda c: (len(wanted & set(c[0].view.pvs)), c[0].rows.count))
        if lingering:
            del self._lingering[best.id]
            best.adopt(view, gate=gate)
            return best
        store = TableStore(view, self._table, self._port())
        store.adopt(view, source=best, gate=gate)
        return store

    def _linger(self, store: TableStore) -> None:
        self._lingering[store.id] = (store, self._clock_ns())
        self._sweep_lingering()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(self._table.linger_ns / 1e9 + 0.05, self._sweep_lingering)

    def _sweep_lingering(self) -> None:
        now = self._clock_ns()
        for sid, (store, since) in list(self._lingering.items()):
            if now - since >= self._table.linger_ns:
                del self._lingering[sid]
                self._drop_store(store)
        while len(self._lingering) > self._table.max_lingering:
            sid = next(iter(self._lingering))
            store, _ = self._lingering.pop(sid)
            self._drop_store(store)

    def _drop_store(self, store: TableStore) -> None:
        store.cancel()
        if store.holder is not None:
            self._spawn_unpin(store.holder)
            store.holder = None

    def _spawn_unpin(self, holder: str) -> None:
        if self._unpin is None:
            return
        try:
            task = asyncio.ensure_future(self._unpin(holder))
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _table_loop(self, shared: _SharedEval) -> None:
        store, view = shared.store, shared.view
        interval_s = shared.interval_ms / 1000.0
        try:
            # Live ingest for every input, from this task -- never from the WS read
            # loop, and never with the broker's seed wait (b7:CF-1).
            try:
                if self._ensure_capacity is not None:
                    for pv in view.pvs:
                        self._ensure_capacity(pv)
                await self._subscribe_table_inputs(list(view.pvs))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("table %s: live subscribe failed", shared.key[:8], exc_info=True)
            finally:
                shared.ingest_ready.set()
            if store.busy:
                await self._post_wait(shared, f"loading history for {len(view.pvs)} input(s)")
            await store.wait_ready()
            store.tick()
            await self._emit_table(shared)  # always one result once the seed (or copy) is done
            while True:
                await asyncio.sleep(interval_s)
                try:
                    now = self._clock_ns()
                    if not store.busy:
                        due = store.due_retries(now)
                        if due:
                            store.start_retry(due)
                    store.tick(now)
                    if store.version != shared.sent_version:
                        await self._emit_table(shared)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("table %s tick failed: %s", shared.key[:8], exc, exc_info=True)
                    await self._post_wait(shared, f"table evaluation failed: {exc}", ErrorCode.UNHANDLED_EXCEPTION)
        except asyncio.CancelledError:
            pass

    async def _subscribe_table_inputs(self, pvs: list[str]) -> None:
        """Vole live ingest without the broker's seed wait (a dead PV must not hold the eval)."""
        await self._subscribe_without_seed_wait(self._subscribe_live, pvs)

    @staticmethod
    async def _subscribe_without_seed_wait(subscribe: SubscribeFn, pvs: list[str]) -> None:
        try:
            await subscribe(pvs, wait_seed=False)
        except TypeError as exc:
            if "wait_seed" not in str(exc):
                raise
            await subscribe(pvs)  # an injected subscribe without the keyword

    async def _post_wait(
        self, shared: _SharedEval, message: str, code: ErrorCode = ErrorCode.FORMULA_INPUT_MISSING
    ) -> None:
        head = {"type": "error", "code": code.value, "message": message, "retryable": True}
        last = shared.last_frame
        if isinstance(last, _EncodedFrame) and last.head == head:
            return
        self._post(shared, _encode_head(head))

    async def _emit_table(self, shared: _SharedEval) -> None:
        store = shared.store
        if store is None:
            return
        shared.sent_version = store.version
        frame = store.frame()  # copies, taken on the loop (b5:IA-1)
        data = await asyncio.to_thread(encode_table_data, frame)
        shared.waiting_since = None
        self._post(shared, _EncodedFrame(head={"type": "result"}, rest=f'"data":{data}'))

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
        if listener.wake is not None:  # table: into the slot, unless a newer frame is already there
            if listener.pending is None:
                listener.pending = shared.last_frame
                listener.wake.set()
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
        # Per code: a transient failure keeps the client "waiting" while the loop
        # carries on; a deterministic one (parse, type, ...) is final.
        await self._broadcast(
            shared, {"type": "error", "code": code, "message": message, "retryable": is_retryable(code)}
        )

    async def _note_input_wait(self, shared: _SharedEval, exc: CanopyError) -> None:
        """Handle a retryable failure: the caller retries, the client hears once.

        The first ``input_wait_notify_s`` of a wait stay silent -- on a fresh
        subscribe the inputs normally land within milliseconds. After that the
        listeners get one error frame flagged ``retryable``; it is re-sent only
        if the reason changes, never on every retry. A ``FormulaInputOutage``
        skips the silence.
        """
        now = time.monotonic()
        if shared.waiting_since is None:
            shared.waiting_since = now
        # An open outage is not a value on its way: the input's connection is down
        # now, so the client hears at once (still once per reason).
        if not isinstance(exc, FormulaInputOutage) and now - shared.waiting_since < self._input_wait_notify_s:
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


def _table_settings(formula_service: FormulaService) -> TableSettings:
    cfg = getattr(formula_service, "config", None)
    if cfg is None:
        return TableSettings()
    return TableSettings(
        settle_ns=int(cfg.table_settle_horizon_ms) * 1_000_000,
        late_window_ns=int(cfg.table_late_window_s * 1e9),
        retry_ns=int(cfg.table_retry_s * 1e9),
        linger_ns=int(cfg.table_linger_s * 1e9),
        poll_ms=int(cfg.table_poll_ms),
        max_per_client=int(cfg.table_max_per_client),
        max_lingering=int(cfg.table_max_lingering),
    )


def _no_record(pv: str, ts_ns: int) -> None:
    return None


def _no_records(pv: str, recv_after_ns: int) -> list:
    return []


def _empty_slice(pv: str, ts_from_ns: int) -> RingSlice:
    return RingSlice(np.empty(0, np.int64), np.empty(0, dtype=object), np.empty(0, np.int8), [])
