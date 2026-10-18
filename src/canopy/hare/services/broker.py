"""PVBroker — the in-process live-PV broker.

Owns the connector, the subscription registry, a latest-value cache, and three
background loops (consume / flush / heartbeat). Monitors are started and stopped
on demand: the first subscriber to a PV starts an EPICS monitor; once the last
one leaves, the monitor and its cached value stay warm for
``monitor_grace_seconds`` and are then released. Updates are buffered and
flushed as diffs every ``ws_batch_interval_ms`` to the clients subscribed to
each PV.

A PV without a cached value is seeded with a one-shot get and describe, run
concurrently. Seeds are shared between clients, bounded by ``seed_concurrency``
across the broker, and each PV's ``initial`` frame goes out as soon as its own
seed lands, so an unreachable name delays only itself. A name whose seed came
back disconnected is remembered for ``negative_cache_ttl_s``, and a held PV
whose seed left its value or metadata missing is re-seeded by the first
subscribe after ``seed_retry_backoff_s``.
"""

import time
import asyncio
import inspect
import logging
from typing import Protocol, runtime_checkable
from collections import defaultdict
from dataclasses import dataclass
from collections.abc import Mapping, Callable, Iterable, Awaitable

from canopy.epics.base import PVConnector, Subscription
from canopy.core.errors import ErrorCode
from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, PVValue, PVWriteValue
from canopy.contracts.base import CanopyModel
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import InvalidPVName, validate_pv_name
from canopy.hare.services.subscriptions import SubscriptionRegistry

LifecycleHook = Callable[[list[str]], Awaitable[None]]

logger = logging.getLogger(__name__)

# Write keywords handed to a connector only when its ``put`` accepts them.
_WRITE_CONTEXT_KEYWORDS = ("binding", "origin")

# Negative-cache size at which expired entries are swept on insert.
_NEGATIVE_SWEEP_AT = 1024

# What a describe that failed or timed out leaves behind: bare metadata (b6:IA-1).
_EMPTY_META_FIELDS = PVMeta(pv="").model_dump(exclude={"pv"})


@dataclass(frozen=True)
class _DeadName:
    """A name whose seed read came back disconnected, and when to forget it."""

    expires: float
    value: PVValue
    meta: PVMeta | None


def _accepted_write_keywords(connector: PVConnector) -> frozenset[str]:
    """Which of `_WRITE_CONTEXT_KEYWORDS` the connector's ``put`` accepts (``**kwargs`` takes all)."""
    try:
        params = inspect.signature(connector.put).parameters.values()
    except (TypeError, ValueError):
        return frozenset()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return frozenset(_WRITE_CONTEXT_KEYWORDS)
    names = {
        p.name for p in params if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    return frozenset(name for name in _WRITE_CONTEXT_KEYWORDS if name in names)


@runtime_checkable
class ClientChannel(Protocol):
    """What the broker needs from a connected client (FastAPI WebSocket fits)."""

    async def send_json(self, data) -> None: ...
    async def close(self) -> None: ...


class PVBroker:
    """Fan live PV updates from one connector out to subscribed WebSocket clients.

    Parameters
    ----------
    connector : PVConnector
        Backend used for monitors, one-shot reads, describes and writes.
    config : HareConfig
        Batching, heartbeat, resource-cap and seeding settings.
    """

    def __init__(self, connector: PVConnector, config: HareConfig) -> None:
        self._connector = connector
        self._config = config
        # Inspected once: a connector's put signature does not change at runtime.
        self._put_keywords = _accepted_write_keywords(connector)
        self._registry = SubscriptionRegistry()
        self._channels: dict[str, ClientChannel] = {}
        self._monitors: dict[str, Subscription] = {}
        self._latest: dict[str, PVValue] = {}
        self._meta: dict[str, PVMeta] = {}
        self._buffer: dict[str, PVValue] = {}
        self._buffer_lock = asyncio.Lock()
        # Bounded so a fast producer can't grow the queue without limit; the
        # QueueFull path in _on_event then applies backpressure by dropping.
        self._queue: asyncio.Queue[PVValue] = asyncio.Queue(maxsize=config.event_queue_maxsize)
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._activation_hooks: list[tuple[LifecycleHook, set[str]]] = []
        self._deactivation_hooks: list[tuple[LifecycleHook, set[str]]] = []
        # Seeding: one shared task per PV being seeded, a broker-wide bound on
        # concurrent seeds, and per-client tasks that deliver landed seeds.
        self._seed_sem = asyncio.Semaphore(max(1, config.seed_concurrency))
        self._seeding: dict[str, asyncio.Task] = {}
        self._deliveries: dict[str, set[asyncio.Task]] = {}
        self._negative: dict[str, _DeadName] = {}
        # Held PVs whose last seed left a half missing: no re-seed before this time.
        self._seed_retry_after: dict[str, float] = {}
        # PVs with no subscriber kept warm until their release task fires.
        self._grace: dict[str, asyncio.Task] = {}
        # Late-connect re-describes in flight, one per PV (b6:IA-1).
        self._redescribing: dict[str, asyncio.Task] = {}

    @property
    def registry(self) -> SubscriptionRegistry:
        return self._registry

    # --- lifecycle ------------------------------------------------------- #

    async def start(self) -> None:
        if self._running:
            return
        await self._connector.start()
        self._running = True
        self._tasks = [self._spawn_loop(name) for name in ("consume", "flush", "heartbeat")]
        logger.info(
            "PVBroker started (backend=%s, batch=%dms)",
            getattr(self._connector, "backend_name", "?"),
            self._config.ws_batch_interval_ms,
        )

    _LOOP_FACTORIES: dict[str, str] = {
        "consume": "_consume_loop",
        "flush": "_flush_loop",
        "heartbeat": "_heartbeat_loop",
    }

    def _spawn_loop(self, name: str) -> asyncio.Task:
        """Create a supervised background-loop task."""
        coro = getattr(self, self._LOOP_FACTORIES[name])()
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._supervise_loop)
        return task

    def _supervise_loop(self, task: asyncio.Task) -> None:
        """Done-callback: a background loop should only finish on shutdown.

        The loop bodies already swallow per-iteration errors, so a task
        reaching here while the broker is still running means an unexpected
        failure in the loop scaffolding itself. Log it loudly (never silent)
        and restart the loop so diffs/heartbeats keep flowing to every client.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        name = task.get_name()
        logger.error("broker loop %r exited unexpectedly; restarting", name, exc_info=exc)
        if self._running:
            self._tasks = [self._spawn_loop(name) if t is task else t for t in self._tasks]

    async def stop(self) -> None:
        self._running = False
        background = [*self._tasks, *self._grace.values(), *self._seeding.values(), *self._redescribing.values()]
        background += [t for tasks in self._deliveries.values() for t in tasks]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        self._tasks = []
        self._grace.clear()
        self._seeding.clear()
        self._redescribing.clear()
        self._deliveries.clear()
        for pv, sub in list(self._monitors.items()):
            try:
                await sub.close()
            except Exception:
                logger.exception("error closing monitor for %s", pv)
        self._monitors.clear()
        for client_id in list(self._channels):
            channel = self._channels.pop(client_id, None)
            if channel is not None:
                try:
                    await channel.close()
                except Exception:
                    pass
        await self._connector.close()
        logger.info("PVBroker stopped")

    # --- client lifecycle ------------------------------------------------ #

    def add_client(self, client_id: str, channel: ClientChannel) -> None:
        self._channels[client_id] = channel
        self._registry.add_client(client_id)

    async def remove_client(self, client_id: str) -> None:
        self._cancel_deliveries(client_id)
        affected_pvs = list(self._registry.client_pvs(client_id))
        deactivated = self._registry.remove_client(client_id)
        await self._release_monitors(deactivated)
        if affected_pvs:
            await self._fire_deactivation_hooks(affected_pvs, client_id)
        self._channels.pop(client_id, None)

    @staticmethod
    def _accept_names(pvs: list[str]) -> tuple[list[str], list[str]]:
        """Split a subscribe batch into ``(accepted, rejected)`` names, logging each drop.

        A single bad name (empty, over-long, whitespace- or protocol-prefixed
        garbage) must not abort the whole batch or reach the EPICS connector, so
        it is skipped here rather than raised - the rest of the client's PVs
        still subscribe. Rejected names are returned so the caller can tell the
        client which ones were dropped instead of leaving them to look like PVs
        that simply never produced a value.
        """
        accepted: list[str] = []
        rejected: list[str] = []
        for pv in pvs:
            try:
                accepted.append(validate_pv_name(pv))
            except InvalidPVName as exc:
                logger.warning("ignoring invalid PV name in subscribe: %s", exc)
                rejected.append(pv)
        return accepted, rejected

    def _apply_caps(self, client_id: str, pvs: list[str]) -> list[str]:
        """Enforce the per-message and per-client subscription caps.

        Returns the accepted names: already-subscribed PVs pass through
        (idempotent, no budget cost), new PVs are admitted up to the client's
        remaining budget. Excess is dropped with a warning so one client can't
        exhaust the shared broker's memory / EPICS connections.
        """
        if len(pvs) > self._config.max_pvs_per_message:
            logger.warning(
                "subscribe batch of %d exceeds per-message cap %d; truncating",
                len(pvs),
                self._config.max_pvs_per_message,
            )
            pvs = pvs[: self._config.max_pvs_per_message]

        existing = self._registry.client_pvs(client_id)
        budget = max(0, self._config.max_subscriptions_per_client - len(existing))
        accepted: list[str] = []
        seen: set[str] = set()
        new_admitted = 0
        dropped = 0
        for pv in pvs:
            if pv in seen:
                continue
            seen.add(pv)
            if pv in existing:
                accepted.append(pv)
            elif new_admitted < budget:
                accepted.append(pv)
                new_admitted += 1
            else:
                dropped += 1
        if dropped:
            logger.warning(
                "client %s hit subscription cap %d; dropped %d PV(s)",
                client_id,
                self._config.max_subscriptions_per_client,
                dropped,
            )
        return accepted

    async def subscribe(self, client_id: str, pvs: list[str], *, wait_seed: bool = True) -> list[str]:
        """Subscribe a client to PVs and send it their cached values and metadata.

        Invalid names and names past the caps are dropped rather than raised, so
        callers must use the return value rather than assume the whole request
        was honoured. Invalid names are reported to the client in an
        ``invalid_pv_name`` error frame.

        PVs answerable from cache are sent in one ``initial`` frame before this
        returns. The rest are seeded, and each one's ``initial`` frame is sent as
        soon as its own seed lands. With ``wait_seed`` the call returns once
        every seed has been delivered; without it, it returns right after
        registration and the seeds are delivered in the background, which is
        what keeps a socket's read loop free while a dead name times out.

        A PV whose last seed left its value disconnected or its metadata missing
        is answered from cache until its ``seed_retry_backoff_s`` window has
        elapsed; the next subscribe after that re-seeds whichever half is still
        missing, so a quiet record that came up is picked up even though its
        monitor never ticked. Concurrent subscribes of the same PV share one
        seed attempt.

        Parameters
        ----------
        client_id : str
            Identifier of a client registered with `add_client`.
        pvs : list[str]
            Requested PV names; may contain duplicates and invalid names.
        wait_seed : bool, optional
            Return only once every seed has been delivered (default).

        Returns
        -------
        list[str]
            The PV names subscribed after validation, de-duplication and cap
            enforcement.
        """
        pvs, rejected = self._accept_names(pvs)
        if rejected:
            await self._send(
                client_id,
                {
                    "type": "error",
                    "code": ErrorCode.INVALID_PV_NAME.value,
                    "message": f"rejected {len(rejected)} invalid PV name(s)",
                    "pvNames": rejected,
                },
            )
        if not pvs:
            return []
        pvs = self._apply_caps(client_id, pvs)
        if not pvs:
            return []
        previously_subscribed = self._registry.client_pvs(client_id)
        newly = self._registry.subscribe(client_id, pvs)
        self._cancel_release(newly)
        await self._start_monitors(newly)
        new_for_client = [pv for pv in pvs if pv not in previously_subscribed]
        if new_for_client:
            await self._fire_activation_hooks(new_for_client, client_id)

        to_seed = [pv for pv in pvs if self._needs_seed(pv)]
        if len(to_seed) < len(pvs):
            seeding = set(to_seed)
            await self._send_initial(client_id, [pv for pv in pvs if pv not in seeding])
        if to_seed:
            delivery = self._deliver_seeds(client_id, to_seed)
            if wait_seed:
                await delivery
            else:
                self._track_delivery(client_id, asyncio.create_task(delivery, name=f"seed-delivery:{client_id}"))
        return pvs

    # --- seeding --------------------------------------------------------- #

    def _needs_seed(self, pv: str) -> bool:
        """True when ``pv`` must be seeded before it can be answered.

        A released name still in the negative cache is restored into the cache
        first, with the entry's expiry as its retry deadline. A PV is then
        answered from cache when its value and metadata are both held (a
        disconnected value counts only when no failed seed left a deadline
        behind), or while its retry deadline is pending.
        """
        if pv not in self._latest:
            entry = self._negative.get(pv)
            if entry is not None:
                if entry.expires > time.monotonic():
                    self._latest[pv] = entry.value
                    if entry.meta is not None:
                        self._meta.setdefault(pv, entry.meta)
                    self._seed_retry_after[pv] = entry.expires
                else:
                    del self._negative[pv]
        value = self._latest.get(pv)
        if value is not None and not self._meta_is_missing(pv):
            if value.connected or pv not in self._seed_retry_after:
                return False
        now = time.monotonic()
        return now >= self._seed_retry_after.get(pv, now)

    def _ensure_seed(self, pv: str) -> asyncio.Task:
        """Return the in-flight seed for ``pv``, starting one if there is none."""
        task = self._seeding.get(pv)
        if task is None:
            task = asyncio.create_task(self._seed_pv(pv), name=f"seed:{pv}")
            self._seeding[pv] = task

            def _forget(done: asyncio.Task, pv: str = pv) -> None:
                if self._seeding.get(pv) is done:
                    del self._seeding[pv]

            task.add_done_callback(_forget)
        return task

    async def _deliver_seeds(self, client_id: str, pvs: list[str]) -> None:
        """Send ``client_id`` an ``initial`` frame for each PV as its seed lands.

        PVs whose seeds complete together share one frame. The waits are
        independent of the seed tasks: cancelling a delivery (the client left)
        leaves the shared seeds running for other subscribers.
        """
        waiting = {self._ensure_seed(pv): pv for pv in pvs}
        pending = set(waiting)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            await self._send_initial(client_id, [waiting[task] for task in done])

    async def _send_initial(self, client_id: str, pvs: list[str]) -> None:
        """Send the cached value + metadata of the PVs the client still holds.

        Always the current cache entry, never a seed's own read: a monitor tick
        that landed first is newer, and a diff carrying it may already be out.
        """
        held = self._registry.client_pvs(client_id)
        pvs = [pv for pv in pvs if pv in held]
        initial = {pv: self._latest[pv] for pv in pvs if pv in self._latest}
        meta = {pv: self._meta[pv] for pv in pvs if pv in self._meta}
        if initial or meta:
            await self._send(
                client_id,
                {
                    "type": "initial",
                    "data": {pv: val.model_dump(by_alias=True) for pv, val in initial.items()},
                    "meta": {pv: m.model_dump(by_alias=True) for pv, m in meta.items()},
                    "count": len(initial),
                },
            )

    def _track_delivery(self, client_id: str, task: asyncio.Task) -> None:
        self._deliveries.setdefault(client_id, set()).add(task)

        def _done(done: asyncio.Task) -> None:
            owned = self._deliveries.get(client_id)
            if owned is not None:
                owned.discard(done)
                if not owned:
                    del self._deliveries[client_id]
            if not done.cancelled() and done.exception() is not None:
                logger.error("seed delivery to %s failed", client_id, exc_info=done.exception())

        task.add_done_callback(_done)

    def _cancel_deliveries(self, client_id: str) -> None:
        current = asyncio.current_task()
        for task in self._deliveries.pop(client_id, set()):
            # A delivery whose send failed is dropping its own client; let it
            # finish that cleanup rather than cancelling it mid-way.
            if task is not current:
                task.cancel()

    def _is_held(self, pv: str) -> bool:
        """True while ``pv`` has a subscriber or is being kept warm."""
        return pv in self._grace or bool(self._registry.subscribers(pv))

    async def _seed_pv(self, pv: str) -> None:
        """Fetch the seed value + static metadata for one PV, get and describe at once.

        Never raises: a failed read becomes a disconnected value (and a negative
        cache entry), so one bad PV can't sink anything else. A value a monitor
        delivered while the read was in flight is newer and is kept.

        Only the missing halves are fetched: the value when none is cached or a
        failed seed left a disconnected one, the metadata when it is absent or
        bare. When either half is still missing afterwards the PV's retry
        deadline is set ``seed_retry_backoff_s`` ahead; when both are held it is
        cleared. `_stop_monitors` drops the deadline with the rest of the PV.
        """
        async with self._seed_sem:
            before = self._latest.get(pv)
            need_get = before is None or (not before.connected and pv in self._seed_retry_after)
            need_meta = self._meta_is_missing(pv)
            if not (need_get or need_meta):
                return
            got, described = await asyncio.gather(
                self._connector.get(pv) if need_get else _nothing(),
                self._connector.describe(pv) if need_meta else _nothing(),
                return_exceptions=True,
            )
        if not self._is_held(pv):
            return  # released while seeding; don't park it in the cache

        meta: PVMeta | None = None
        if need_meta:
            if isinstance(described, BaseException):
                logger.debug("describe failed for %s: %s", pv, described)
            else:
                meta = described
                if self._meta_is_missing(pv):
                    self._meta[pv] = meta
        if need_get:
            self._store_seed_value(pv, before, got, meta)
        latest = self._latest.get(pv)
        if latest is not None and latest.connected and not self._meta_is_missing(pv):
            self._seed_retry_after.pop(pv, None)
        else:
            self._seed_retry_after[pv] = time.monotonic() + self._config.seed_retry_backoff_s

    def _store_seed_value(self, pv: str, before: PVValue | None, got: object, meta: PVMeta | None) -> None:
        """Cache a seed read unless a monitor tick replaced ``before`` meanwhile."""
        if isinstance(got, BaseException):
            logger.debug("initial get failed for %s: %s", pv, got)
            value = PVValue(
                pv=pv,
                value=None,
                severity=AlarmSeverity.INVALID,
                ts_ns=time.time_ns(),
                connected=False,
                error=str(got) or type(got).__name__,
            )
        else:
            value = got
        if self._latest.get(pv) is before:
            self._latest[pv] = value
        if value.connected:
            self._negative.pop(pv, None)
        else:
            self._remember_dead(pv, value, meta)

    def _remember_dead(self, pv: str, value: PVValue, meta: PVMeta | None) -> None:
        ttl = self._config.negative_cache_ttl_s
        if ttl <= 0:
            return
        now = time.monotonic()
        if len(self._negative) >= _NEGATIVE_SWEEP_AT:
            for name in [n for n, e in self._negative.items() if e.expires <= now]:
                del self._negative[name]
        self._negative[pv] = _DeadName(expires=now + ttl, value=value, meta=meta)

    async def unsubscribe(self, client_id: str, pvs: list[str]) -> None:
        if not pvs:
            return
        # Only PVs the client actually held were released here. Firing on the raw
        # request list would let any client deactivate a PV it never subscribed
        # to -- e.g. a REST-warmed one held only by `vole-internal`.
        held = self._registry.client_pvs(client_id)
        released = [pv for pv in pvs if pv in held]
        deactivated = self._registry.unsubscribe(client_id, pvs)
        await self._release_monitors(deactivated)
        if released:
            await self._fire_deactivation_hooks(released, client_id)

    # --- monitor lifecycle ----------------------------------------------- #

    async def _start_monitors(self, pvs: Iterable[str]) -> None:
        for pv in pvs:
            if pv in self._monitors:
                continue
            try:
                self._monitors[pv] = await self._connector.subscribe(pv, self._on_event)
            except Exception as exc:
                logger.error("failed to start monitor for %s: %s", pv, exc)

    async def _release_monitors(self, pvs: Iterable[str]) -> None:
        """Release PVs that lost their last subscriber, after the grace period."""
        grace = self._config.monitor_grace_seconds
        if grace <= 0:
            await self._stop_monitors(pvs)
            return
        for pv in pvs:
            if pv not in self._grace:
                self._grace[pv] = asyncio.create_task(self._release_after_grace(pv, grace), name=f"grace:{pv}")

    async def _release_after_grace(self, pv: str, grace: float) -> None:
        await asyncio.sleep(grace)
        if self._grace.get(pv) is asyncio.current_task():
            del self._grace[pv]
        if self._registry.subscribers(pv):
            return  # re-subscribed without cancelling us; keep it
        try:
            await self._stop_monitors([pv])
        except Exception:
            # Nothing awaits this task, so an unlogged error would be invisible.
            logger.exception("releasing %s after its grace period failed", pv)

    def _cancel_release(self, pvs: Iterable[str]) -> None:
        """A PV in its grace period got a subscriber again: keep it warm."""
        for pv in pvs:
            task = self._grace.pop(pv, None)
            if task is not None:
                task.cancel()

    async def _stop_monitors(self, pvs: Iterable[str]) -> None:
        """Close the monitors of released PVs and discard their cached state.

        A monitor that fails to close is logged and does not stop the rest of
        the batch. The cached value, static metadata and seed retry deadline of
        every given PV are dropped whether or not it had a monitor, so a later
        subscribe seeds the PV afresh (or from the negative cache).

        Parameters
        ----------
        pvs : Iterable[str]
            PVs that have no subscribers left.
        """
        for pv in pvs:
            # Drop the cache before awaiting the close, so a re-subscribe that
            # lands during it seeds afresh instead of having its value erased.
            self._latest.pop(pv, None)
            self._meta.pop(pv, None)
            self._seed_retry_after.pop(pv, None)
            sub = self._monitors.pop(pv, None)
            if sub is not None:
                try:
                    await sub.close()
                except Exception:
                    logger.exception("error closing monitor for %s", pv)

    # --- event ingestion ------------------------------------------------- #

    def _on_event(self, value: PVValue) -> None:
        """Connector callback. Non-blocking; must run on the event loop thread."""
        try:
            self._queue.put_nowait(value)
        except asyncio.QueueFull:
            logger.warning("broker queue full; dropping update for %s", value.pv)

    async def _consume_loop(self) -> None:
        while self._running:
            try:
                value = await self._queue.get()
                await self._ingest(value)
                await self._drain_nowait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("broker consume loop iteration failed")

    async def _drain_nowait(self) -> None:
        """Process all currently-queued events without blocking. Drives `_consume_loop`; also called directly by tests."""
        while True:
            try:
                value = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._ingest(value)

    async def _ingest(self, value: PVValue) -> None:
        previous = self._latest.get(value.pv)
        self._latest[value.pv] = value
        if value.connected:
            self._negative.pop(value.pv, None)
            if (previous is None or not previous.connected) and self._meta_is_missing(value.pv):
                self._ensure_redescribe(value.pv)
        async with self._buffer_lock:
            self._buffer[value.pv] = value

    # --- late-connect re-describe ---------------------------------------- #

    def _meta_is_missing(self, pv: str) -> bool:
        """True when the cached meta is absent, empty or a dead-name placeholder."""
        meta = self._meta.get(pv)
        return meta is None or meta.model_dump(exclude={"pv"}) == _EMPTY_META_FIELDS

    def _ensure_redescribe(self, pv: str) -> None:
        """Spawn one deduped re-describe for ``pv``; nothing awaits a describe on the consume loop."""
        if pv in self._redescribing:
            return
        task = asyncio.create_task(self._redescribe(pv), name=f"redescribe:{pv}")
        self._redescribing[pv] = task

        def _forget(done: asyncio.Task, pv: str = pv) -> None:
            if self._redescribing.get(pv) is done:
                del self._redescribing[pv]
            if not done.cancelled() and done.exception() is not None:
                logger.error("re-describe of %s failed", pv, exc_info=done.exception())

        task.add_done_callback(_forget)

    async def _redescribe(self, pv: str) -> None:
        """Describe a held PV on its first successful connect and re-send ``initial`` to its holders.

        Waits for an in-flight seed first (its describe may land the meta), runs
        under the seed semaphore like ``_seed_pv``, and drops the result when the
        PV was released meanwhile.
        """
        seed = self._seeding.get(pv)
        if seed is not None:
            await asyncio.wait({seed})
        if not self._meta_is_missing(pv) or not self._is_held(pv):
            return
        async with self._seed_sem:
            try:
                meta = await self._connector.describe(pv)
            except Exception as exc:
                logger.debug("re-describe failed for %s: %s", pv, exc)
                return
        if not self._is_held(pv):
            return
        self._meta[pv] = meta
        for client_id in list(self._registry.subscribers(pv)):
            await self._send_initial(client_id, [pv])

    async def _flush_loop(self) -> None:
        interval = self._config.ws_batch_interval_ms / 1000.0
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("broker flush loop iteration failed")

    async def _flush(self) -> None:
        """Send the buffered diffs, then a ``flush`` marker to the channels that ask for one.

        The marker goes out on every flush, empty ones included, and after the
        diff it covers, so an in-process client (Vole) can take its ingest
        watermark from it. Browser sockets never receive it.
        """
        async with self._buffer_lock:
            updates = self._buffer
            self._buffer = {}
        if updates:
            per_client: dict[str, dict[str, dict]] = defaultdict(dict)
            for pv, value in updates.items():
                payload = value.model_dump(by_alias=True)
                for client_id in self._registry.subscribers(pv):
                    per_client[client_id][pv] = payload
            for client_id, data in per_client.items():
                frame = {"type": "diff", "data": data, "count": len(data), "timestamp": time.time()}
                await self._send(client_id, frame)
        marker = {"type": "flush", "timestamp": time.time()}
        for client_id, channel in list(self._channels.items()):
            if getattr(channel, "wants_flush_marker", False):
                await self._send(client_id, marker)

    def heartbeat_frame(self) -> dict:
        """The heartbeat frame: backend plus the connector's identity.

        ``label``, ``notice`` and ``binding`` come from the connector's ``label``,
        ``notice`` and ``binding_id`` attributes, read on every call (``None`` when
        the connector has none), so a change of target shows on the next frame.
        One builder for the heartbeat loop, the first frame of every ``ws_pvs``
        socket and ``GET /health/summary``.
        """
        connector = self._connector
        return {
            "type": "heartbeat",
            "timestamp": time.time(),
            "backend": getattr(connector, "backend_name", None),
            "label": getattr(connector, "label", None),
            "notice": getattr(connector, "notice", None),
            "binding": getattr(connector, "binding_id", None),
        }

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._config.heartbeat_interval_s)
            try:
                message = self.heartbeat_frame()
                for client_id in list(self._channels):
                    await self._send(client_id, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("broker heartbeat loop iteration failed")

    async def _send(self, client_id: str, message: dict) -> None:
        channel = self._channels.get(client_id)
        if channel is None:
            return
        try:
            await channel.send_json(message)
        except Exception as exc:
            logger.debug("send to %s failed: %s; dropping client", client_id, exc)
            self._channels.pop(client_id, None)
            self._cancel_deliveries(client_id)
            affected_pvs = list(self._registry.client_pvs(client_id))
            deactivated = self._registry.remove_client(client_id)
            await self._release_monitors(deactivated)
            if affected_pvs:
                await self._fire_deactivation_hooks(affected_pvs, client_id)

    # --- reads ----------------------------------------------------------- #

    async def get_value(self, pv: str) -> PVValue:
        """Current value: cached if actively monitored, else a one-shot read."""
        validate_pv_name(pv)
        cached = self._latest.get(pv)
        if cached is not None:
            return cached
        return await self._connector.get(pv)

    async def get_describe(self, pv: str) -> PVMeta:
        """Static metadata: cached if actively monitored, else a one-shot describe."""
        validate_pv_name(pv)
        cached = self._meta.get(pv)
        if cached is not None:
            return cached
        return await self._connector.describe(pv)

    # --- writes ---------------------------------------------------------- #

    async def put(
        self,
        pv: str,
        value: PVWriteValue,
        *,
        wait: bool = True,
        timeout: float | None = None,
        binding: str | None = None,
        origin: Mapping | CanopyModel | None = None,
    ) -> None:
        """Write a value to a PV (one-shot, independent of any subscription).

        Delegates to the connector, which routes by protocol prefix and raises
        `PVWriteFailed` on failure or timeout. On success the cached latest value
        is dropped so an immediate `get_value` reflects the write instead of the
        pre-write cache. The monitor path repopulates the cache on its next tick.

        ``binding`` and ``origin`` (passed on as a plain dict) reach the connector
        only when its ``put`` accepts them; a backend that does not check the
        control target never sees them.
        """
        validate_pv_name(pv)
        extra: dict = {}
        if "binding" in self._put_keywords:
            extra["binding"] = binding
        if "origin" in self._put_keywords:
            if isinstance(origin, CanopyModel):
                origin = origin.model_dump()
            extra["origin"] = dict(origin) if origin is not None else None
        await self._connector.put(pv, value, wait=wait, timeout=timeout, **extra)
        self._latest.pop(pv, None)

    # --- introspection --------------------------------------------------- #

    def status(self) -> dict:
        return {
            "running": self._running,
            "backend": getattr(self._connector, "backend_name", None),
            "clients": len(self._channels),
            "active_monitors": len(self._monitors),
            "warm_monitors": len(self._grace),
            "connected": self._connected_count(),
            **self._registry.stats(),
        }

    def _connected_count(self) -> int:
        """Of the active monitors, how many whose latest value is connected.

        A monitor with no value yet counts as not connected: nothing has said
        the channel is there. ``active_monitors`` is the total it is read
        against.
        """
        return sum(1 for pv in self._monitors if (latest := self._latest.get(pv)) is not None and latest.connected)

    # --- lifecycle hooks ------------------------------------------------- #

    def add_activation_hook(self, hook: LifecycleHook, *, exclude_clients: set[str] | None = None) -> None:
        self._activation_hooks.append((hook, exclude_clients or set()))

    def remove_activation_hook(self, hook: LifecycleHook) -> None:
        self._activation_hooks = [(h, ex) for h, ex in self._activation_hooks if h is not hook]

    def add_deactivation_hook(self, hook: LifecycleHook, *, exclude_clients: set[str] | None = None) -> None:
        self._deactivation_hooks.append((hook, exclude_clients or set()))

    def remove_deactivation_hook(self, hook: LifecycleHook) -> None:
        self._deactivation_hooks = [(h, ex) for h, ex in self._deactivation_hooks if h is not hook]

    async def _fire_activation_hooks(self, pvs: list[str], client_id: str) -> None:
        for hook, exclude in self._activation_hooks:
            if client_id in exclude:
                continue
            relevant = self._filter_newly_active_for_hook(pvs, exclude)
            if relevant:
                try:
                    await hook(relevant)
                except Exception:
                    logger.exception("activation hook %r failed", hook)

    async def _fire_deactivation_hooks(self, pvs: list[str], client_id: str) -> None:
        for hook, exclude in self._deactivation_hooks:
            if client_id in exclude:
                continue
            relevant = self._filter_deactivated_for_hook(pvs, exclude)
            if relevant:
                try:
                    await hook(relevant)
                except Exception:
                    logger.exception("deactivation hook %r failed", hook)

    def _filter_newly_active_for_hook(self, pvs: list[str], exclude: set[str]) -> list[str]:
        """Return PVs that just became active from the hook's perspective.

        A PV is "newly active" for a hook if it now has exactly one
        subscriber that is NOT in the hook's exclude set (meaning the
        subscribe that just happened was the first external one).
        """
        result = []
        for pv in pvs:
            subs = self._registry.subscribers(pv)
            external = subs - exclude
            if len(external) == 1:
                result.append(pv)
        return result

    def _filter_deactivated_for_hook(self, pvs: list[str], exclude: set[str]) -> list[str]:
        """Return PVs that are deactivated from the hook's perspective.

        A PV is "deactivated" for a hook when it has zero subscribers
        outside the hook's exclude set.
        """
        result = []
        for pv in pvs:
            subs = self._registry.subscribers(pv)
            external = subs - exclude
            if not external:
                result.append(pv)
        return result


async def _nothing() -> None:
    return None
