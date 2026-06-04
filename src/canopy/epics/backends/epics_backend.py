"""EPICS connector backend.

Channel Access via ``aioca``, PV Access via ``p4p``, routed by ``ca://`` /
``pva://`` prefix (unprefixed -> CA, with optional PVA fallback). Monitor callbacks
from the EPICS libraries are marshaled onto the asyncio loop via ``call_soon_threadsafe``
before reaching the broker, so a high update rate never spawns per-update tasks.
"""

import math
import time
import asyncio
import logging
from dataclasses import dataclass

from aioca import FORMAT_TIME, CANothing, caget, camonitor
from p4p.client.asyncio import Context as P4PContext

from canopy.epics.base import Subscription, EventCallback, normalize_severity
from canopy.contracts.pv import PVValue
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import is_unprefixed, parse_pv_name

logger = logging.getLogger(__name__)


def _sanitize(value):
    """Make a value JSON-safe: NaN/Inf -> None, recurse into lists."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _to_py(value):
    """Convert numpy scalars/arrays to plain Python."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


class EpicsConnector:
    backend_name = "epics"

    def __init__(
        self,
        ca_timeout_s: float = 5.0,
        pva_timeout_s: float = 5.0,
        unprefixed_pva_fallback: bool = False,
    ) -> None:
        self._ca_timeout = ca_timeout_s
        self._pva_timeout = pva_timeout_s
        self._fallback = unprefixed_pva_fallback
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pva_context = None

    @classmethod
    def from_config(cls, cfg) -> "EpicsConnector":
        return cls(
            ca_timeout_s=cfg.ca_timeout_s,
            pva_timeout_s=cfg.pva_timeout_s,
            unprefixed_pva_fallback=cfg.unprefixed_pva_fallback,
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

    async def close(self) -> None:
        if self._pva_context is not None:
            try:
                self._pva_context.close()
            except Exception:
                pass
            self._pva_context = None

    def _get_pva_context(self):
        if self._pva_context is None:
            self._pva_context = P4PContext("pva", nt=False)
        return self._pva_context

    # --- one-shot reads -------------------------------------------------- #

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            return await self._get_pva(pv, stripped, timeout)
        result = await self._get_ca(pv, stripped, timeout)
        if not result.connected and self._fallback and is_unprefixed(pv):
            pva = await self._get_pva(pv, stripped, timeout)
            if pva.connected:
                return pva
        return result

    async def _get_ca(self, pv: str, stripped: str, timeout: float | None) -> PVValue:
        t = timeout if timeout is not None else self._ca_timeout
        try:
            aug = await caget(stripped, format=FORMAT_TIME, timeout=t, throw=False)
        except Exception as exc:
            return self._disconnected(pv, str(exc))
        return self._ca_to_pvvalue(pv, aug)

    async def _get_pva(self, pv: str, stripped: str, timeout: float | None) -> PVValue:
        t = timeout if timeout is not None else self._pva_timeout
        try:
            ctx = self._get_pva_context()
            value = await asyncio.wait_for(ctx.get(stripped), timeout=t)
        except Exception as exc:
            return self._disconnected(pv, str(exc))
        return self._pva_to_pvvalue(pv, value)

    # --- monitors -------------------------------------------------------- #

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            return self._subscribe_pva(pv, stripped, on_event)
        return self._subscribe_ca(pv, stripped, on_event)

    def _subscribe_ca(self, pv: str, stripped: str, on_event: EventCallback) -> Subscription:
        loop = self._loop or asyncio.get_event_loop()

        def callback(value, *_args):
            if isinstance(value, CANothing):
                pvvalue = self._disconnected(pv, "disconnected from IOC")
            else:
                pvvalue = self._ca_to_pvvalue(pv, value)
            loop.call_soon_threadsafe(on_event, pvvalue)

        subscription = camonitor(stripped, callback, notify_disconnect=True, format=FORMAT_TIME)
        return _CaSubscription(subscription)

    def _subscribe_pva(self, pv: str, stripped: str, on_event: EventCallback) -> Subscription:
        ctx = self._get_pva_context()
        loop = self._loop or asyncio.get_event_loop()

        def callback(value):
            if isinstance(value, Exception):
                pvvalue = self._disconnected(pv, str(value))
            else:
                pvvalue = self._pva_to_pvvalue(pv, value)
            loop.call_soon_threadsafe(on_event, pvvalue)

        monitor = ctx.monitor(stripped, callback)
        return _PvaSubscription(monitor)

    # --- conversions ----------------------------------------------------- #

    def _ca_to_pvvalue(self, pv: str, aug) -> PVValue:
        if not getattr(aug, "ok", True):
            return self._disconnected(pv, f"CA read failed: {getattr(aug, 'errorcode', 'unknown')}")
        raw_ts = getattr(aug, "timestamp", None)
        ts_ns = int(float(raw_ts) * 1_000_000_000) if raw_ts else time.time_ns()
        return PVValue(
            pv=pv,
            value=_sanitize(_to_py(aug)),
            severity=normalize_severity(getattr(aug, "severity", None)),
            ts_ns=ts_ns,
            connected=True,
            units=getattr(aug, "units", None),
            precision=getattr(aug, "precision", None),
        )

    def _pva_to_pvvalue(self, pv: str, value) -> PVValue:
        raw = None
        severity = None
        units = None
        ts_ns = time.time_ns()
        try:
            if hasattr(value, "get"):
                raw = value.get("value")
                severity = value.get("alarm.severity")
                units = value.get("display.units")
                secs = value.get("timeStamp.secondsPastEpoch")
                nanos = value.get("timeStamp.nanoseconds") or 0
                if secs is not None:
                    ts_ns = int(secs) * 1_000_000_000 + int(nanos)
        except Exception:
            pass
        if raw is None:
            raw = value
        return PVValue(
            pv=pv,
            value=_sanitize(_to_py(raw)),
            severity=normalize_severity(severity),
            ts_ns=ts_ns,
            connected=True,
            units=units or None,
        )

    def _disconnected(self, pv: str, error: str) -> PVValue:
        return PVValue(
            pv=pv,
            value=None,
            severity=AlarmSeverity.INVALID,
            ts_ns=time.time_ns(),
            connected=False,
            error=error,
        )


@dataclass
class _CaSubscription:
    sub: object

    async def close(self) -> None:
        try:
            self.sub.close()
        except Exception:
            pass


@dataclass
class _PvaSubscription:
    sub: object

    async def close(self) -> None:
        try:
            self.sub.close()
        except Exception:
            pass
