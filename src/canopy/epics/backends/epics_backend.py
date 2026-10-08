"""EPICS connector backend.

Channel Access via ``aioca``, PV Access via ``p4p``, routed by ``ca://`` /
``pva://`` prefix (unprefixed -> CA, with optional PVA fallback). Monitor callbacks
from the EPICS libraries are marshaled onto the asyncio loop via ``call_soon_threadsafe``
before reaching the broker, so a high update rate never spawns per-update tasks.

Each value is classified into a `ValueKind` by its declared EPICS type — the PV
Access Normative-Type id (``value.getID()``) or the Channel Access DBR type — so a
client can distinguish scalar / string / enum / waveform without inspecting the
payload.
"""

import math
import time
import asyncio
import logging
from dataclasses import dataclass

from aioca import (
    FORMAT_CTRL,
    FORMAT_TIME,
    CANothing,
    caget,
    caput,
    cainfo,
    _catools,
    camonitor,
)
from p4p.client.asyncio import Context as P4PContext

from canopy.epics.base import (
    Subscription,
    EventCallback,
    PVWriteFailed,
    normalize_severity,
)
from canopy.contracts.pv import PVMeta, PVValue, ValueKind, TableValue, PVWriteValue
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import is_unprefixed, parse_pv_name, validate_pv_name

logger = logging.getLogger(__name__)

# EPICS DBF/DBR field-type codes (aioca exposes them via the augmented value's
# datatype): STRING=0, ENUM=3, CHAR=4.
_DBR_STRING = 0
_DBR_ENUM = 3
_DBR_CHAR = 4

# PV Access Normative-Type short names
_PVA_KINDS = {
    "NTScalar": ValueKind.SCALAR,
    "NTScalarArray": ValueKind.WAVEFORM,
    "NTEnum": ValueKind.ENUM,
    "NTTable": ValueKind.TABLE,
}


def _ca_channels() -> dict | None:
    """aioca's table of open channels for the running loop, by PV name, or None
    when it has opened none yet.

    aioca keeps every channel it ever opened and offers no way to drop a single
    one, so this reaches into its private cache. A change there surfaces as an
    exception in `EpicsConnector._ca_search_afresh`, which then turns itself off."""
    cache = _catools._Context._channel_caches.get(asyncio.get_running_loop())  # noqa: SLF001
    if cache is None:
        return None
    return cache._ChannelCache__channels  # noqa: SLF001


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


def _finite(value) -> float | None:
    """A finite float, else None (drops NaN/Inf limits)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _decode_char_array(values) -> str:
    """Decode a CA DBR_CHAR waveform as the EPICS long-string idiom: the bytes up
    to the first NUL."""
    if isinstance(values, bytes | bytearray):
        raw = bytes(values)
    elif isinstance(values, list):
        raw = bytes(int(v) & 0xFF for v in values if v is not None)
    else:
        return str(values)
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")


def _pva_get(value, field):
    """``value.get(field)`` that returns None for a missing/odd field."""
    try:
        return value.get(field)
    except Exception:
        return None


def _pva_kind_and_id(value) -> tuple[ValueKind, str]:
    """Classify a p4p Value by its Normative-Type id. Unknown-but-recognized NT
    types (NTNDArray/NTMatrix/...) -> UNSUPPORTED; an id-less plain structure is
    treated as a scalar and refined later by its Python value type."""
    try:
        type_id = value.getID() or ""
    except Exception:
        type_id = ""
    short = type_id.split("/")[-1].split(":")[0] if type_id else ""
    if short in _PVA_KINDS:
        return _PVA_KINDS[short], type_id
    if short == "":
        return ValueKind.SCALAR, type_id
    return ValueKind.UNSUPPORTED, type_id


class EpicsConnector:
    backend_name = "epics"

    def __init__(
        self,
        ca_timeout_s: float = 5.0,
        pva_timeout_s: float = 5.0,
        unprefixed_pva_fallback: bool = False,
        char_array_as_string: bool = True,
        max_array_elements: int = 100_000,
        connect_report_s: float = 1.5,
        ca_reconnect_s: float = 15.0,
    ) -> None:
        self._ca_timeout = ca_timeout_s
        self._pva_timeout = pva_timeout_s
        self._fallback = unprefixed_pva_fallback
        self._char_array_as_string = char_array_as_string
        self._max_array_elements = max_array_elements
        self._connect_report = connect_report_s
        self._ca_reconnect = ca_reconnect_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pva_context = None
        # Channel Access reconnection (see `_ca_search_afresh`), all by bare PV
        # name: this connector's open monitors, when the channel was last opened
        # or dropped here, and the pending timer of each lost monitored channel.
        self._ca_monitors: dict[str, set[_CaSubscription]] = {}
        self._ca_searched: dict[str, float] = {}
        self._ca_watchers: dict[str, asyncio.TimerHandle] = {}

    @classmethod
    def from_config(cls, cfg) -> "EpicsConnector":
        return cls(
            ca_timeout_s=cfg.ca_timeout_s,
            pva_timeout_s=cfg.pva_timeout_s,
            unprefixed_pva_fallback=cfg.unprefixed_pva_fallback,
            char_array_as_string=cfg.char_array_as_string,
            max_array_elements=cfg.max_array_elements,
            connect_report_s=cfg.connect_report_s,
            ca_reconnect_s=cfg.ca_reconnect_s,
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

    async def close(self) -> None:
        for timer in self._ca_watchers.values():
            timer.cancel()
        self._ca_watchers.clear()
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

    # --- Channel Access reconnection -------------------------------------- #

    def _ca_search_afresh(self, name: str, *, min_age_s: float) -> None:
        """Drop the channel for `name` if it is lost, so its next use searches anew.

        libca keeps searching for a channel whose server went away, but ever more
        slowly: measured against a soft IOC, the searches come 12, 16, 49 and 98 s
        apart after the first half minute, and the gap keeps doubling up to
        ``EPICS_CA_MAX_SEARCH_PERIOD`` (300 s). Only a server beacon resets that,
        and a client that reaches its server through an address list (a container,
        a gateway) sees none. Until the next search every read times out, although
        the server is back. A new channel is searched for at once, which is why
        restarting the process helps.

        So a lost channel is closed and forgotten here, and the caller's next use
        of the name opens a new one. This connector's monitors on it are reopened.
        The channel is left alone when it was opened or dropped less than
        `min_age_s` ago, and when it carries a subscription this connector did not
        open, which closing the channel would end."""
        if self._ca_reconnect <= 0:
            return
        now = time.monotonic()
        try:
            channels = _ca_channels()
            channel = channels.get(name) if channels is not None else None
            if channel is None:
                # The caller is about to open it: its search starts now.
                self._ca_searched[name] = now
                return
            if channel.connected():
                self._ca_searched.pop(name, None)
                return
            if now - self._ca_searched.get(name, -math.inf) < min_age_s:
                return
            monitors = list(self._ca_monitors.get(name, ()))
            if channel.count_subscriptions() != sum(1 for m in monitors if m.attached()):
                return
            for monitor in monitors:
                monitor.detach()
            try:
                del channels[name]
                channel._purge()  # noqa: SLF001
                self._ca_searched[name] = now
            finally:
                for monitor in monitors:
                    monitor.attach(connect_timeout=None)
        except Exception:
            logger.exception(
                "dropping the lost CA channel %s failed; lost channels are left to libca's own search from here on",
                name,
            )
            self._ca_reconnect = 0

    def _ca_watch(self, name: str) -> None:
        """Start the timer that keeps searching for a lost monitored channel.

        A read drops a lost channel itself, but a monitored PV may never be read:
        its last event is what callers are shown."""
        if self._ca_reconnect <= 0 or name in self._ca_watchers or not self._ca_monitors.get(name):
            return
        loop = asyncio.get_running_loop()
        self._ca_watchers[name] = loop.call_later(self._ca_reconnect, self._ca_watch_tick, name)

    def _ca_watch_tick(self, name: str) -> None:
        self._ca_watchers.pop(name, None)
        if not self._ca_monitors.get(name):
            return
        # Half the period: a read may have dropped the channel a moment ago.
        self._ca_search_afresh(name, min_age_s=self._ca_reconnect / 2)
        try:
            channels = _ca_channels()
            lost = channels is not None and name in channels and not channels[name].connected()
        except Exception:
            logger.exception("checking the CA channel %s failed", name)
            lost = False
        if lost:
            # A reconnect ends the watch; the next disconnect event restarts it.
            self._ca_watch(name)

    # --- one-shot reads -------------------------------------------------- #

    async def get(self, pv: str, *, timeout: float | None = None) -> PVValue:
        validate_pv_name(pv)
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
        self._ca_search_afresh(stripped, min_age_s=self._ca_timeout)
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

    # --- writes ---------------------------------------------------------- #

    async def put(self, pv: str, value: PVWriteValue, *, timeout: float | None = None, wait: bool = True) -> None:
        """Write `value` to `pv`, routed by protocol prefix."""
        validate_pv_name(pv)
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            await self._put_pva(pv, stripped, value, timeout, wait)
        else:
            await self._put_ca(pv, stripped, value, timeout, wait)

    async def _put_ca(self, pv: str, stripped: str, value: PVWriteValue, timeout: float | None, wait: bool) -> None:
        t = timeout if timeout is not None else self._ca_timeout
        self._ca_search_afresh(stripped, min_age_s=self._ca_timeout)
        try:
            result = await caput(stripped, value, wait=wait, timeout=t, throw=False)
        except Exception as exc:
            raise PVWriteFailed(f"CA write to {pv} failed: {exc}") from exc
        if not getattr(result, "ok", True):
            raise PVWriteFailed(f"CA write to {pv} failed: {result}")

    async def _put_pva(self, pv: str, stripped: str, value: PVWriteValue, timeout: float | None, wait: bool) -> None:
        t = timeout if timeout is not None else self._pva_timeout
        try:
            ctx = self._get_pva_context()
            await asyncio.wait_for(ctx.put(stripped, value, wait=wait), timeout=t)
        except Exception as exc:
            raise PVWriteFailed(f"PVA write to {pv} failed: {exc}") from exc

    # --- metadata (describe) --------------------------------------------- #

    async def describe(self, pv: str) -> PVMeta:
        validate_pv_name(pv)
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            return await self._describe_pva(pv, stripped)
        return await self._describe_ca(pv, stripped)

    async def _describe_ca(self, pv: str, stripped: str) -> PVMeta:
        self._ca_search_afresh(stripped, min_age_s=self._ca_timeout)
        try:
            aug = await caget(stripped, format=FORMAT_CTRL, timeout=self._ca_timeout, throw=False)
        except Exception:
            return PVMeta(pv=pv)
        if not getattr(aug, "ok", True):
            return PVMeta(pv=pv)
        precision = getattr(aug, "precision", None)
        meta = PVMeta(
            pv=pv,
            value_kind=self._ca_kind(aug),
            units=(getattr(aug, "units", None) or None),
            precision=int(precision) if precision is not None else None,
            display_low=_finite(getattr(aug, "lower_disp_limit", None)),
            display_high=_finite(getattr(aug, "upper_disp_limit", None)),
            control_low=_finite(getattr(aug, "lower_ctrl_limit", None)),
            control_high=_finite(getattr(aug, "upper_ctrl_limit", None)),
            warning_low=_finite(getattr(aug, "lower_warning_limit", None)),
            warning_high=_finite(getattr(aug, "upper_warning_limit", None)),
            alarm_low=_finite(getattr(aug, "lower_alarm_limit", None)),
            alarm_high=_finite(getattr(aug, "upper_alarm_limit", None)),
        )
        enums = getattr(aug, "enums", None)
        if enums is not None and len(enums) > 0:
            meta.enum_choices = [str(e) for e in enums]
        try:
            info = await cainfo(stripped, timeout=self._ca_timeout)
            meta.writable = bool(getattr(info, "write", None))
        except Exception:
            pass
        return meta

    async def _describe_pva(self, pv: str, stripped: str) -> PVMeta:
        try:
            ctx = self._get_pva_context()
            value = await asyncio.wait_for(ctx.get(stripped), timeout=self._pva_timeout)
        except Exception:
            return PVMeta(pv=pv)
        kind, _ = _pva_kind_and_id(value)
        precision = _pva_get(value, "display.precision")
        meta = PVMeta(
            pv=pv,
            value_kind=kind,
            units=(_pva_get(value, "display.units") or None),
            precision=int(precision) if precision is not None else None,
            description=(_pva_get(value, "display.description") or None),
            display_low=_finite(_pva_get(value, "display.limitLow")),
            display_high=_finite(_pva_get(value, "display.limitHigh")),
            control_low=_finite(_pva_get(value, "control.limitLow")),
            control_high=_finite(_pva_get(value, "control.limitHigh")),
            warning_low=_finite(_pva_get(value, "valueAlarm.lowWarningLimit")),
            warning_high=_finite(_pva_get(value, "valueAlarm.highWarningLimit")),
            alarm_low=_finite(_pva_get(value, "valueAlarm.lowAlarmLimit")),
            alarm_high=_finite(_pva_get(value, "valueAlarm.highAlarmLimit")),
            form=self._pva_form(value),
        )
        if kind is ValueKind.ENUM:
            choices = _pva_get(value, "value.choices")
            if choices:
                meta.enum_choices = [str(c) for c in choices]
        elif kind is ValueKind.SCALAR and isinstance(_to_py(_pva_get(value, "value")), str):
            meta.value_kind = ValueKind.STRING
        return meta

    @staticmethod
    def _pva_form(value) -> str | None:
        choices = _pva_get(value, "display.form.choices")
        idx = _pva_get(value, "display.form.index")
        try:
            if choices and idx is not None:
                return str(choices[int(idx)])
        except (IndexError, TypeError, ValueError):
            pass
        return None

    # --- monitors -------------------------------------------------------- #

    async def subscribe(self, pv: str, on_event: EventCallback) -> Subscription:
        validate_pv_name(pv)
        protocol, stripped = parse_pv_name(pv)
        if protocol == "pva":
            return self._subscribe_pva(pv, stripped, on_event)
        return self._subscribe_ca(pv, stripped, on_event)

    def _subscribe_ca(self, pv: str, stripped: str, on_event: EventCallback) -> Subscription:
        loop = self._loop or asyncio.get_event_loop()

        def deliver(pvvalue: PVValue) -> None:
            # On the loop thread. A lost channel is searched for from here on.
            if not pvvalue.connected:
                self._ca_watch(stripped)
            on_event(pvvalue)

        def callback(value, *_args):
            # Runs on aioca's C worker thread. An exception here would propagate
            # into libca's callback dispatch with no Python frame above it to
            # catch it, which can abort the process -- so nothing may escape.
            try:
                if isinstance(value, CANothing):
                    pvvalue = self._disconnected(pv, "disconnected from IOC")
                else:
                    pvvalue = self._ca_to_pvvalue(pv, value)
                loop.call_soon_threadsafe(deliver, pvvalue)
            except Exception:
                logger.exception("CA monitor callback for %s failed", pv)

        def open_monitor(connect_timeout: float | None):
            # `connect_timeout` makes aioca report a channel that has not connected
            # within the bound as one disconnect; it still updates if it connects later.
            return camonitor(
                stripped,
                callback,
                notify_disconnect=True,
                format=FORMAT_TIME,
                connect_timeout=connect_timeout,
            )

        subscription = _CaSubscription(open_monitor, self._ca_monitors, stripped)
        subscription.attach(connect_timeout=self._connect_report)
        return subscription

    def _subscribe_pva(self, pv: str, stripped: str, on_event: EventCallback) -> Subscription:
        ctx = self._get_pva_context()

        # p4p's asyncio Context requires a coroutine callback and invokes it on the
        # loop thread, so on_event can be called directly without marshaling.
        connected_once = False

        def report_not_connected():
            # The channel has not connected within the bound: say so once. The
            # monitor stays open, so a PV that appears later updates normally.
            if connected_once:
                return
            try:
                on_event(self._disconnected(pv, f"not connected within {self._connect_report:g} s"))
            except Exception:
                logger.exception("PVA not-connected report for %s failed", pv)

        loop = self._loop or asyncio.get_event_loop()
        timer = loop.call_later(self._connect_report, report_not_connected)

        async def callback(value):
            nonlocal connected_once
            # p4p invokes this as a task on the loop; an unhandled exception
            # would become an unretrieved-task error and silently wedge this
            # PV's monitor. Guard the whole body.
            try:
                if isinstance(value, Exception):
                    # p4p reports the initial not-yet-connected state before the first
                    # value. Suppress that so a live PV doesn't flicker disconnected on
                    # subscribe. Only report drops once we've actually connected; a
                    # channel that never connects is reported by the timer above.
                    if not connected_once:
                        return
                    pvvalue = self._disconnected(pv, str(value))
                else:
                    connected_once = True
                    pvvalue = self._pva_to_pvvalue(pv, value)
                on_event(pvvalue)
            except Exception:
                logger.exception("PVA monitor callback for %s failed", pv)

        monitor = ctx.monitor(stripped, callback, notify_disconnect=True)
        return _PvaSubscription(monitor, timer)

    # --- conversions ----------------------------------------------------- #

    def _ca_kind(self, aug) -> ValueKind:
        dt = getattr(aug, "datatype", None)
        count = getattr(aug, "element_count", None) or 1
        enums = getattr(aug, "enums", None)
        if dt == _DBR_ENUM or (enums is not None and len(enums) > 0):
            return ValueKind.ENUM
        if dt == _DBR_STRING:
            return ValueKind.WAVEFORM if count > 1 else ValueKind.STRING
        if dt == _DBR_CHAR and count > 1:
            return ValueKind.STRING if self._char_array_as_string else ValueKind.WAVEFORM
        if count > 1:
            return ValueKind.WAVEFORM
        return ValueKind.SCALAR

    def _truncate(self, values: list) -> tuple[list, str | None]:
        if len(values) > self._max_array_elements:
            n = len(values)
            return (
                values[: self._max_array_elements],
                f"array truncated to {self._max_array_elements} of {n} elements",
            )
        return values, None

    def _ca_to_pvvalue(self, pv: str, aug) -> PVValue:
        if not getattr(aug, "ok", True):
            return self._disconnected(pv, f"CA read failed: {getattr(aug, 'errorcode', 'unknown')}")
        raw_ts = getattr(aug, "timestamp", None)
        ts_ns = int(float(raw_ts) * 1_000_000_000) if raw_ts else time.time_ns()
        kind = self._ca_kind(aug)
        py = _to_py(aug)
        error = None
        if kind is ValueKind.ENUM:
            try:
                value = int(py)
            except (TypeError, ValueError):
                value = py
        elif kind is ValueKind.STRING:
            value = _decode_char_array(py) if isinstance(py, list) else py
        elif kind is ValueKind.WAVEFORM and isinstance(py, list):
            py, error = self._truncate(py)
            value = _sanitize(py)
        else:
            value = _sanitize(py)
        return PVValue(
            pv=pv,
            value=value,
            value_kind=kind,
            severity=normalize_severity(getattr(aug, "severity", None)),
            ts_ns=ts_ns,
            connected=True,
            error=error,
        )

    def _pva_to_pvvalue(self, pv: str, value) -> PVValue:
        ts_ns = time.time_ns()
        severity = _pva_get(value, "alarm.severity")
        secs = _pva_get(value, "timeStamp.secondsPastEpoch")
        nanos = _pva_get(value, "timeStamp.nanoseconds") or 0
        if secs:
            ts_ns = int(secs) * 1_000_000_000 + int(nanos)

        kind, type_id = _pva_kind_and_id(value)
        error = None
        if kind is ValueKind.UNSUPPORTED:
            raw = None
            error = f"unsupported PV type: {type_id or 'unknown'}"
        elif kind is ValueKind.ENUM:
            idx = _pva_get(value, "value.index")
            raw = int(idx) if idx is not None else None
        elif kind is ValueKind.TABLE:
            raw, error = self._pva_table(value)
        else:
            raw = _to_py(_pva_get(value, "value"))
            if isinstance(raw, list):
                raw, error = self._truncate(raw)
                kind = ValueKind.WAVEFORM
            elif isinstance(raw, str):
                kind = ValueKind.STRING
            raw = _sanitize(raw)

        return PVValue(
            pv=pv,
            value=raw,
            value_kind=kind,
            severity=normalize_severity(severity),
            ts_ns=ts_ns,
            connected=True,
            error=error,
        )

    def _pva_table(self, value) -> tuple[TableValue, str | None]:
        """Extract an NTTable into a column-major TableValue. Columns are read
        from the value sub-structure in declared order. The headers parallel them,
        falling back to the column field names when absent."""
        labels = [str(x) for x in (_pva_get(value, "labels") or [])]
        try:
            col_map = value.todict("value") or {}
        except Exception:
            col_map = {}
        if not labels:
            labels = list(col_map.keys())
        error = None
        columns: list[list] = []
        for col in col_map.values():
            col = _to_py(col)
            if isinstance(col, list):
                col, err = self._truncate(col)
                error = error or err
                col = _sanitize(col)
            else:
                col = [col]
            columns.append(col)
        return TableValue(labels=labels, columns=columns), error

    def _disconnected(self, pv: str, error: str) -> PVValue:
        return PVValue(
            pv=pv,
            value=None,
            severity=AlarmSeverity.INVALID,
            ts_ns=time.time_ns(),
            connected=False,
            error=error,
        )


class _CaSubscription:
    """One Channel Access monitor. Its aioca subscription can be replaced while
    it stays open, which is how it moves to a new channel after the old one was
    lost (`EpicsConnector._ca_search_afresh`)."""

    def __init__(self, open_monitor, registry: dict[str, set["_CaSubscription"]], name: str) -> None:
        self._open_monitor = open_monitor
        self._registry = registry
        self._name = name
        self._closed = False
        self.sub = None

    def attached(self) -> bool:
        """Whether the aioca subscription is registered on its channel, which it
        is once the channel has connected for the first time."""
        return self.sub is not None and self.sub.state == self.sub.OPEN

    def attach(self, *, connect_timeout: float | None) -> None:
        if self._closed:
            return
        self.sub = self._open_monitor(connect_timeout)
        self._registry.setdefault(self._name, set()).add(self)

    def detach(self) -> None:
        sub, self.sub = self.sub, None
        if sub is not None:
            try:
                sub.close()
            except Exception:
                pass

    async def close(self) -> None:
        self._closed = True
        monitors = self._registry.get(self._name)
        if monitors is not None:
            monitors.discard(self)
            if not monitors:
                del self._registry[self._name]
        self.detach()


@dataclass
class _PvaSubscription:
    sub: object
    timer: asyncio.TimerHandle | None = None

    async def close(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        try:
            self.sub.close()
        except Exception:
            pass
