"""PVBroker — the in-process live-PV broker.

Owns the connector, the subscription registry, a latest-value cache, and three
background loops (consume / flush / heartbeat). Monitors are started and stopped
on demand: the first subscriber to a PV starts an EPICS monitor, the last to
leave stops it. Updates are buffered and flushed as diffs every
``ws_batch_interval_ms`` to the clients subscribed to each PV.
"""

import time
import asyncio
import logging
from typing import Protocol, runtime_checkable
from collections import defaultdict
from collections.abc import Iterable

from canopy.epics.base import PVConnector, Subscription
from canopy.core.errors import ErrorCode
from canopy.hare.config import HareConfig
from canopy.contracts.pv import PVMeta, PVValue, PVWriteValue
from canopy.epics.pv_protocol import InvalidPVName, validate_pv_name
from canopy.hare.services.subscriptions import SubscriptionRegistry

logger = logging.getLogger(__name__)


@runtime_checkable
class ClientChannel(Protocol):
    """What the broker needs from a connected client (FastAPI WebSocket fits)."""

    async def send_json(self, data) -> None: ...
    async def close(self) -> None: ...


class PVBroker:
    def __init__(self, connector: PVConnector, config: HareConfig) -> None:
        self._connector = connector
        self._config = config
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
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
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
        deactivated = self._registry.remove_client(client_id)
        await self._stop_monitors(deactivated)
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

    async def subscribe(self, client_id: str, pvs: list[str]) -> None:
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
            return
        pvs = self._apply_caps(client_id, pvs)
        if not pvs:
            return
        newly = self._registry.subscribe(client_id, pvs)
        await self._start_monitors(newly)
        # Seed latest value + static metadata for newly-activated PVs so the
        # subscriber gets an immediate value (and describe info) rather than
        # waiting for the first monitor tick. Seed concurrently -- but bounded by
        # a semaphore so a huge batch can't open thousands of connections at once
        # -- so one slow or unreachable PV can't stall the rest of the batch.
        sem = asyncio.Semaphore(self._config.seed_concurrency)

        async def _seed(pv: str) -> None:
            async with sem:
                await self._seed_pv(pv)

        await asyncio.gather(*(_seed(pv) for pv in newly))
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

    async def _seed_pv(self, pv: str) -> None:
        """Fetch the seed value + static metadata for one newly-activated PV.

        Per-PV failures are logged and swallowed so that one bad PV doesn't sink
        the concurrent seed of the rest of the batch.
        """
        if pv not in self._latest:
            try:
                self._latest[pv] = await self._connector.get(pv)
            except Exception as exc:
                logger.debug("initial get failed for %s: %s", pv, exc)
        if pv not in self._meta:
            try:
                self._meta[pv] = await self._connector.describe(pv)
            except Exception as exc:
                logger.debug("describe failed for %s: %s", pv, exc)

    async def unsubscribe(self, client_id: str, pvs: list[str]) -> None:
        if not pvs:
            return
        deactivated = self._registry.unsubscribe(client_id, pvs)
        await self._stop_monitors(deactivated)

    # --- monitor lifecycle ----------------------------------------------- #

    async def _start_monitors(self, pvs: Iterable[str]) -> None:
        for pv in pvs:
            if pv in self._monitors:
                continue
            try:
                self._monitors[pv] = await self._connector.subscribe(pv, self._on_event)
            except Exception as exc:
                logger.error("failed to start monitor for %s: %s", pv, exc)

    async def _stop_monitors(self, pvs: Iterable[str]) -> None:
        for pv in pvs:
            sub = self._monitors.pop(pv, None)
            if sub is not None:
                try:
                    await sub.close()
                except Exception:
                    logger.exception("error closing monitor for %s", pv)
            self._latest.pop(pv, None)
            self._meta.pop(pv, None)

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
        self._latest[value.pv] = value
        async with self._buffer_lock:
            self._buffer[value.pv] = value

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
        async with self._buffer_lock:
            if not self._buffer:
                return
            updates = self._buffer
            self._buffer = {}
        per_client: dict[str, dict[str, dict]] = defaultdict(dict)
        for pv, value in updates.items():
            payload = value.model_dump(by_alias=True)
            for client_id in self._registry.subscribers(pv):
                per_client[client_id][pv] = payload
        for client_id, data in per_client.items():
            frame = {"type": "diff", "data": data, "count": len(data), "timestamp": time.time()}
            await self._send(client_id, frame)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._config.heartbeat_interval_s)
            try:
                message = {
                    "type": "heartbeat",
                    "timestamp": time.time(),
                    "backend": getattr(self._connector, "backend_name", None),
                }
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
            deactivated = self._registry.remove_client(client_id)
            await self._stop_monitors(deactivated)

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

    async def put(self, pv: str, value: PVWriteValue, *, wait: bool = True, timeout: float | None = None) -> None:
        """Write a value to a PV (one-shot, independent of any subscription).

        Delegates to the connector, which routes by protocol prefix and raises
        `PVWriteFailed` on failure or timeout. On success the cached latest value
        is dropped so an immediate `get_value` reflects the write instead of the
        pre-write cache. The monitor path repopulates the cache on its next tick.
        """
        validate_pv_name(pv)
        await self._connector.put(pv, value, wait=wait, timeout=timeout)
        self._latest.pop(pv, None)

    # --- introspection --------------------------------------------------- #

    def status(self) -> dict:
        return {
            "running": self._running,
            "backend": getattr(self._connector, "backend_name", None),
            "clients": len(self._channels),
            "active_monitors": len(self._monitors),
            **self._registry.stats(),
        }
