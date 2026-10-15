"""Per-shared-eval row store for live ``table`` formulas (FR2-FR4).

A live table keeps its own rows instead of re-aligning a window on every poll:

* **Rows.** One shared index -- the samples of the trigger binding (Trigger PV
  mode) or multiples of Δt (Timer mode) -- and one ``float64`` per input per row
  in preallocated column rings (NaN = null), ``N`` deep. Columns are computed from
  the input arrays (a bare ``{binding}`` column *is* its input), so carried-over
  history belongs to a PV, never to a role slot.
* **Seed.** On first use the store fills itself back to ``N`` rows or the
  lookback, from the Vole ring where it reaches back and from the archive for the
  older part, each cell taking the *newer* of the two (b6:CF-2). Seeding runs in
  a store-owned task chain, so a store that lingers after its last listener left
  keeps seeding and a new eval takes the running seed over (b7:OC-1).
* **Tick.** New index points become rows; a row is *final* once the ingest
  watermark is ``H`` past its arrival, or every input has moved past it. Newer
  rows are a *provisional tail* recomputed every tick.
* **Late data.** Per input, a live sample that is not after-connect and either
  arrived for an already-final row or shows an IOC clock off by more than ``H``
  marks the input ``lateAfterFinal`` for 60 s of receive time.

Everything here runs on the event loop except the bulk cell fills and the frame
encode, which go to worker threads.
"""

from __future__ import annotations

import re
import math
import asyncio
import logging
import itertools
from typing import Any
from dataclasses import replace, dataclass
from collections.abc import Callable, Awaitable

import numpy as np
import orjson

from canopy.fox.ports import RingSlice, SampleRecord
from canopy.contracts.sample import AlarmSeverity
from canopy.epics.pv_protocol import strip_protocol_prefix
from canopy.mole.adapters.base import (
    SEVERITY_ORDINAL,
    SEED_SAMPLE_BUDGET,
    FIRST_SEED_WINDOW_NS,
    LAST_BEFORE_MAX_TIMESTAMPS,
    ArchiveBlock,
    PVNotFoundInArchiver,
    ArchiverNotConfigured,
)
from canopy.fox.services.parser import ParsedFormula
from canopy.fox.services.evaluator import (
    bare_binding,
    _evaluate_array_asteval,
    _evaluate_array_numexpr,
)

logger = logging.getLogger(__name__)

_INVALID = SEVERITY_ORDINAL[AlarmSeverity.INVALID]
#: Hare flushes every 100 ms; seeded and ring-backfilled trigger samples are
#: coalesced to the last one per bucket, matching live ingest (b4:CC-13).
FLUSH_BUCKET_NS = 100_000_000
#: How many non-numeric ring samples an edge seed walks back over for its predecessor (A19).
_PREV_WALK = 64
#: Above this many samples a cell fill runs in a worker thread.
_THREAD_FILL_THRESHOLD = 2_000
#: The far future, for "latest sample" lookups.
_FAR_FUTURE = 2**62

# The CPU part of seeds runs at most two at a time (r3:CC-4); one semaphore per
# event loop, since an asyncio primitive binds to the loop it first waited on.
_SEED_CPU: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


# Synchronous ring copies (an O(k) Python copy each) run one per loop iteration,
# so seeds of many inputs never stack them into one long event-loop stall (b7:CC-3).
_RING_GATE: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _per_loop(registry: dict, factory):
    loop = asyncio.get_running_loop()
    obj = registry.get(loop)
    if obj is None:
        for stale in [lp for lp in registry if lp.is_closed()]:
            del registry[stale]
        obj = registry[loop] = factory()
    return obj


def _seed_cpu() -> asyncio.Semaphore:
    return _per_loop(_SEED_CPU, lambda: asyncio.Semaphore(2))


async def _ring_read(fn, *args):
    """Run one synchronous ring read, then yield, so the next one lands in a later loop iteration."""
    async with _per_loop(_RING_GATE, asyncio.Lock):
        result = fn(*args)
        # Yield while still holding the gate: a read queued behind this one then
        # runs in a later loop iteration, never back-to-back in this one.
        await asyncio.sleep(0)
    return result


History = str  # "seeded" | "live-only" | "not-archived" | "truncated"


# --------------------------------------------------------------------------- #
# Settings and port
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TableSettings:
    settle_ns: int = 2_000_000_000
    late_window_ns: int = 60_000_000_000
    retry_ns: int = 60_000_000_000
    linger_ns: int = 60_000_000_000
    poll_ms: int = 500
    max_per_client: int = 8
    max_lingering: int = 64


@dataclass
class TablePort:
    """The Vole/Mole callables a table store reads through (1.2's port)."""

    last_at_or_before: Callable[[str, int], SampleRecord | None]
    samples_since: Callable[[str, int], list[SampleRecord]]
    ring_slice: Callable[[str, int], RingSlice]
    now_ns: Callable[[], int]
    watermark: Callable[[], int] | None = None
    fetch_window_budgeted: Callable[..., Awaitable[ArchiveBlock]] | None = None
    fetch_last_before: Callable[..., Awaitable[list]] | None = None
    event_faithful: Callable[..., Awaitable[bool]] | None = None


# --------------------------------------------------------------------------- #
# Spec view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColumnDef:
    name: str
    # Bare column: the canonical PV it passes through. Expression column: None.
    pv: str | None
    parsed: ParsedFormula | None = None
    # Placeholder variable (``__v<i>``) -> canonical PV, for expression columns.
    variables: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TableView:
    """What one table spec needs from a store, with every binding resolved to its PV."""

    bindings: dict[str, str]  # binding name -> canonical PV
    pvs: tuple[str, ...]  # unique canonical PVs, binding order
    columns: tuple[ColumnDef, ...]
    trigger_pv: str | None
    grid_dt_ns: int | None
    n: int
    lookback_ns: int
    # Trigger PV mode only: "rising" / "falling" fires rows on that edge (A19);
    # None is Every update, the plain rule.
    trigger_edge: str | None = None

    @property
    def index_key(self) -> str:
        """Stores are reused and copied only between views with the same key.

        An edge rule gets its own ``trigger-<edge>:`` prefix, so its rows, held
        trigger value and persisted predecessor never cross into an Every update
        store (whose key, ``trigger:<pv>``, is unchanged) or another edge's, and no
        PV name can make an edgeless key equal an edge key.
        """
        if self.trigger_pv is None:
            return f"grid:{self.grid_dt_ns}"
        if self.trigger_edge is None:
            return f"trigger:{self.trigger_pv}"
        return f"trigger-{self.trigger_edge}:{self.trigger_pv}"


_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


def build_view(spec: Any, parsed_columns: dict[str, ParsedFormula | None], lookback_ns: int) -> TableView:
    """Resolve a validated table ``FormulaSpec``; ``parsed_columns`` holds None for bare columns."""
    bindings = {name: strip_protocol_prefix(pv) for name, pv in spec.bindings.items()}
    columns: list[ColumnDef] = []
    for name, expression in spec.columns.items():
        bare = bare_binding(expression, spec.bindings)
        if bare is not None:
            columns.append(ColumnDef(name=name, pv=bindings[bare]))
            continue
        parsed = parsed_columns[name]
        variables = tuple((f"__v{i}", bindings[token]) for i, token in enumerate(parsed.tokens))
        columns.append(ColumnDef(name=name, pv=None, parsed=parsed, variables=variables))
    trigger_pv = bindings[spec.trigger] if spec.index == "first_input" else None
    trigger_edge = getattr(spec, "trigger_edge", None) if spec.index == "first_input" else None
    return TableView(
        bindings=bindings,
        pvs=tuple(dict.fromkeys(bindings.values())),
        columns=tuple(columns),
        trigger_pv=trigger_pv,
        grid_dt_ns=spec.grid_dt_ns if spec.index == "grid" else None,
        n=int(spec.max_points),
        lookback_ns=int(lookback_ns),
        trigger_edge=trigger_edge,
    )


def compute_columns(view: TableView, inputs: dict[str, np.ndarray], length: int) -> dict[str, np.ndarray]:
    """Column arrays from input arrays; a bare column is its input array (no copy)."""
    out: dict[str, np.ndarray] = {}
    for col in view.columns:
        if col.pv is not None:
            out[col.name] = inputs[col.pv]
            continue
        out[col.name] = _eval_expression(col, inputs, length)
    return out


def _eval_expression(col: ColumnDef, inputs: dict[str, np.ndarray], length: int) -> np.ndarray:
    if length == 0:
        return np.empty(0, dtype=np.float64)
    variables = {var: inputs[pv] for var, pv in col.variables}
    try:
        with np.errstate(all="ignore"):
            if col.parsed.is_numexpr_compatible:
                result = _evaluate_array_numexpr(col.parsed, variables)
            else:
                result = _evaluate_array_asteval(col.parsed, variables)
        arr = np.asarray(result, dtype=np.float64)
        if arr.shape != (length,):
            arr = np.broadcast_to(arr, (length,)).astype(np.float64)
        else:
            arr = arr.copy()
    except Exception as exc:  # a cell the expression cannot compute is null, never an error frame
        logger.debug("table column %s failed: %s", col.name, exc)
        return np.full(length, np.nan)
    arr[~np.isfinite(arr)] = np.nan
    return arr


# --------------------------------------------------------------------------- #
# Cell conversion (R11, b7:IA-2)
# --------------------------------------------------------------------------- #


def _numeric(value: Any) -> float:
    """int/float/bool (enum index) -> float; anything else (str, bytes, list, dict, None) -> NaN."""
    if isinstance(value, bool | int | float | np.integer | np.floating | np.bool_):
        try:
            f = float(value)
        except OverflowError:
            return math.nan
        return f if math.isfinite(f) else math.nan
    return math.nan


def cell_from_record(rec: SampleRecord | None) -> float:
    """One live cell: null before the first sample, for INVALID, inside an outage, or non-numeric."""
    if rec is None or rec.in_outage or rec.severity is AlarmSeverity.INVALID:
        return math.nan
    return _numeric(rec.value)


_type_of = np.frompyfunc(type, 1, 1)
# Types converted in one C-level pass; anything else goes through ``_numeric`` one by one.
_BULK_KINDS = frozenset({float, int, bool, np.float64, np.float32, np.int64, np.int32, np.bool_})
_is_bulk = np.frompyfunc(_BULK_KINDS.__contains__, 1, 1)


def numeric_array(values: np.ndarray) -> np.ndarray:
    """``_numeric`` over an object array: plain floats/ints/bools in bulk, the rest one by one."""
    n = len(values)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    bulk = _is_bulk(_type_of(values)).astype(bool)
    if bulk.any():
        try:
            with np.errstate(all="ignore"):
                out[bulk] = values[bulk].astype(np.float64)
        except (OverflowError, ValueError, TypeError):  # e.g. an int beyond float range
            bulk[:] = False
    for i in np.flatnonzero(~bulk):
        out[i] = _numeric(values[i])
    out[~np.isfinite(out)] = np.nan
    return out


def _outage_mask(ts: np.ndarray, outages: list[tuple[int, int | None]]) -> np.ndarray:
    mask = np.zeros(len(ts), dtype=bool)
    for start, end in outages:
        if end is None:
            mask |= ts >= start
        else:
            mask |= (ts >= start) & (ts <= end)
    return mask


def _object_array(values: list[Any]) -> np.ndarray:
    """An object array holding ``values`` as-is (a list value stays one element)."""
    out = np.empty(len(values), dtype=object)
    for i, v in enumerate(values):
        out[i] = v
    return out


def _edge_mask(values: np.ndarray, edge: str, prev: float | None) -> tuple[np.ndarray, float | None]:
    """Which of ``values`` (raw trigger samples, time order) fire ``edge`` (A19).

    Each numeric sample is compared with the numeric sample before it -- ``prev``
    for the first one, the store's persisted predecessor (None: the oldest sample
    ever seen, which never fires). bool counts as int; None, str, bytes and any
    other non-numeric value (also NaN / inf) neither fire nor become the
    predecessor. Returns the fire mask and the new predecessor.
    """
    fire = np.zeros(len(values), dtype=bool)
    if not len(values):
        return fire, prev
    v = numeric_array(values)
    idx = np.flatnonzero(~np.isnan(v))
    if not idx.size:
        return fire, prev
    vv = v[idx]
    before = np.concatenate([[math.nan if prev is None else prev], vv[:-1]])
    with np.errstate(invalid="ignore"):
        fire[idx] = vv > before if edge == "rising" else vv < before
    return fire, float(vv[-1])


def _coalesce(ts: np.ndarray, *others: np.ndarray) -> tuple[np.ndarray, ...]:
    """Keep the last sample per flush bucket (``ts`` ascending)."""
    if ts.size == 0:
        return (ts, *others)
    bucket = ts // FLUSH_BUCKET_NS
    keep = np.ones(ts.size, dtype=bool)
    keep[:-1] = bucket[1:] != bucket[:-1]
    return (ts[keep], *(o[keep] for o in others))


# --------------------------------------------------------------------------- #
# Row ring
# --------------------------------------------------------------------------- #


class RowRing:
    """Columnar ring of finalized rows: ``int64`` ts plus one ``float64`` per input PV."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.ts = np.zeros(capacity, dtype=np.int64)
        self.vals: dict[str, np.ndarray] = {}
        self.start = 0
        self.count = 0

    def nbytes(self) -> int:
        return self.ts.nbytes + sum(a.nbytes for a in self.vals.values())

    def _indices(self, k: int) -> np.ndarray:
        first = self.start + self.count - k
        return (first + np.arange(k)) % self.capacity

    def newest(self, k: int) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """Copies of the newest ``k`` rows, oldest first (fancy indexing copies)."""
        k = max(0, min(k, self.count))
        idx = self._indices(k)
        return self.ts[idx], {pv: arr[idx] for pv, arr in self.vals.items()}

    def ordered(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        return self.newest(self.count)

    @property
    def newest_ts(self) -> int | None:
        return int(self.ts[(self.start + self.count - 1) % self.capacity]) if self.count else None

    @property
    def oldest_ts(self) -> int | None:
        return int(self.ts[self.start]) if self.count else None

    def ensure_key(self, pv: str) -> None:
        if pv not in self.vals:
            self.vals[pv] = np.full(self.capacity, np.nan)

    def drop_key(self, pv: str) -> None:
        self.vals.pop(pv, None)

    def append(self, ts: int, row: dict[str, float]) -> None:
        if self.count < self.capacity:
            pos = (self.start + self.count) % self.capacity
            self.count += 1
        else:
            pos = self.start
            self.start = (self.start + 1) % self.capacity
        self.ts[pos] = ts
        for pv, arr in self.vals.items():
            arr[pos] = row.get(pv, math.nan)

    def rebuild(self, ts: np.ndarray, vals: dict[str, np.ndarray], capacity: int | None = None) -> None:
        """Replace every row with ``ts``/``vals`` (ascending), keeping the newest ``capacity``."""
        if capacity is not None:
            self.capacity = capacity
        keep = min(len(ts), self.capacity)
        self.ts = np.zeros(self.capacity, dtype=np.int64)
        self.ts[:keep] = ts[len(ts) - keep :]
        new_vals: dict[str, np.ndarray] = {}
        for pv in set(self.vals) | set(vals):
            arr = np.full(self.capacity, np.nan)
            src = vals.get(pv)
            if src is not None:
                arr[:keep] = src[len(src) - keep :]
            new_vals[pv] = arr
        self.vals = new_vals
        self.start = 0
        self.count = keep

    def set_values(self, pv: str, ts: np.ndarray, values: np.ndarray) -> int:
        """Overwrite ``pv``'s cells for the held rows whose ts is in ``ts``; returns rows changed."""
        if not self.count or pv not in self.vals:
            return 0
        idx = self._indices(self.count)
        held = self.ts[idx]
        pos = np.searchsorted(ts, held)
        pos_c = np.minimum(pos, max(len(ts) - 1, 0))
        hit = (pos < len(ts)) & (ts[pos_c] == held) if len(ts) else np.zeros(len(held), dtype=bool)
        if not hit.any():
            return 0
        arr = self.vals[pv]
        before = arr[idx[hit]].copy()
        arr[idx[hit]] = values[pos_c[hit]]
        return int((~((before == arr[idx[hit]]) | (np.isnan(before) & np.isnan(arr[idx[hit]])))).sum())


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #


@dataclass
class TableFrame:
    """A live table result as plain numpy arrays (never a pydantic model on the live path)."""

    ts_ns: np.ndarray
    columns: dict[str, np.ndarray]
    inputs: dict[str, tuple[str, str, bool]]  # binding -> (history, status, lateAfterFinal)
    total_rows: int


def encode_table_data(frame: TableFrame) -> str:
    """The ``data`` member of a live table result: exactly ``{kind, table}``, NaN/±inf -> null."""
    payload = {
        "kind": "table",
        "table": {
            "tsNs": frame.ts_ns,
            "columns": frame.columns,
            "inputs": {
                b: {"history": h, "status": s, "lateAfterFinal": late} for b, (h, s, late) in frame.inputs.items()
            },
            "totalRows": frame.total_rows,
        },
    }
    return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY).decode()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


@dataclass
class InputState:
    history: History = "seeded"
    # The seed (ring or archive) found a sample for this input at or before a row.
    has_sample: bool = False
    # ``samples_since`` cursor on the receive clock.
    last_seen_recv: int = 0
    last_late_ns: int | None = None
    retry_at: int | None = None


@dataclass
class _Fill:
    values: np.ndarray
    history: History
    has_sample: bool


_store_ids = itertools.count(1)


class TableStore:
    """Rows of one table index (trigger PV | Δt), owned by at most one live eval at a time."""

    def __init__(self, view: TableView, settings: TableSettings, port: TablePort) -> None:
        self.id = next(_store_ids)
        self.index_key = view.index_key
        self.view = view
        self.settings = settings
        self.port = port
        self.rows = RowRing(view.n)
        for pv in view.pvs:
            self.rows.ensure_key(pv)
        now = port.now_ns()
        self.inputs: dict[str, InputState] = {pv: InputState(last_seen_recv=now) for pv in view.pvs}
        self.total_final = 0
        # Provisional tail: row times, arrivals and the visible (non-all-null) part's cells.
        self.tail_ts: list[int] = []
        self.tail_arrival: list[int] = []
        # Seeded cells of tail rows, used where the ring has no sample at or before the row.
        self.tail_base: dict[int, dict[str, float]] = {}
        self._tail_vis_ts = np.empty(0, dtype=np.int64)
        self._tail_vis_vals: dict[str, np.ndarray] = {}
        # Newest index point considered (ts); new trigger samples / grid points start after it.
        self.index_hwm: int | None = None
        self.backfill_pending = True
        self.version = 0
        self._inputs_sig: tuple | None = None
        self.task: asyncio.Task | None = None
        # Vole pin holder id of the eval currently owning this store (or of the lingering store).
        self.holder: str | None = None
        # The oldest row time the store has searched for history down to (None: not yet).
        self.searched_from: int | None = None
        # Archive blocks fetched during the current seed step, per PV.
        self._cache: dict[str, ArchiveBlock] = {}
        self._event_faithful: bool | None = None
        # Edge rule only (A19): the newest numeric trigger sample consumed so far,
        # the predecessor the next sample's edge is decided against (None: none yet).
        self.edge_prev: float | None = None
        # The newest trigger sample a seed step saw (any value): ``(ts, predecessor)``.
        self._seed_seen: tuple[int, float | None] | None = None

    # --- ownership / task chain ---------------------------------------------- #

    def nbytes(self) -> int:
        return self.rows.nbytes()

    def chain(self, make: Callable[[], Awaitable[None]]) -> asyncio.Task:
        """Run ``make()`` after the store's current job; the chain survives its owner."""
        previous = self.task

        async def _run() -> None:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
            await make()

        self.task = asyncio.create_task(_run())
        return self.task

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def wait_ready(self) -> None:
        """Wait for every queued seed step, including ones chained while waiting."""
        while self.task is not None and not self.task.done():
            task = self.task
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is not None:
                logger.warning("table store %d: seed step failed", self.id, exc_info=task.exception())

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    def adopt(
        self, view: TableView, source: TableStore | None = None, gate: asyncio.Event | None = None
    ) -> asyncio.Task:
        """Make this store serve ``view``: copy from ``source`` (if any), then seed what is missing.

        ``gate``, when given, is awaited first: the owning eval opens it once its
        inputs' live ingest is subscribed, so the seed's ring reads see it.
        """
        self.view = view
        for pv in view.pvs:
            if pv not in self.inputs:
                self.inputs[pv] = InputState(last_seen_recv=self.port.now_ns())

        async def _step() -> None:
            if gate is not None:
                await gate.wait()
            await self._reconcile(view, source)

        return self.chain(_step)

    # --- reconcile (seed / carry-over / resize) ------------------------------ #

    async def _reconcile(self, view: TableView, source: TableStore | None) -> None:
        if source is not None:
            await source.wait_ready()
            self._copy_from(source, view)
        if view is not self.view:  # a later adopt superseded this one; it reconciles itself
            return
        now = self.port.now_ns()
        self._cache = {}
        self._event_faithful = None
        # N or lookback change: slice what is held.
        lo = now - view.lookback_ns
        ts, vals = self.rows.ordered()
        if self.rows.count and self.rows.count >= self.rows.capacity:
            # A full ring may have dropped older rows: only what is held is known.
            self.searched_from = max(self.searched_from or self.rows.oldest_ts, self.rows.oldest_ts)
        keep = ts >= lo
        if not keep.all():
            self.searched_from = max(self.searched_from or lo, lo)
        if self.rows.capacity != view.n or not keep.all():
            self.rows.rebuild(ts[keep], {pv: a[keep] for pv, a in vals.items()}, capacity=view.n)
        for pv in list(self.rows.vals):
            if pv not in view.pvs:
                self.rows.drop_key(pv)
        # Inputs the held rows (final and tail) lack: fill them at the held row times.
        missing = [pv for pv in view.pvs if pv not in self.rows.vals]
        for pv in missing:
            self.rows.ensure_key(pv)
        if missing:
            await self._fill_held(missing, now)
        # Older rows up to N / the lookback.
        await self._extend_older(view, now)
        self.backfill_pending = True
        self._cache = {}
        self.version += 1

    async def _fill_held(self, pvs: list[str], now: int) -> None:
        """Seed ``pvs`` at every held row time -- finalized rows and the tail (patch path)."""
        ts, _ = self.rows.ordered()
        tail = np.asarray(self.tail_ts, dtype=np.int64)
        all_ts = np.concatenate([ts, tail]) if tail.size else ts
        if not all_ts.size:
            for pv in pvs:
                self.inputs[pv].history = "seeded"
            return
        order = np.argsort(all_ts, kind="stable")
        sorted_ts = all_ts[order]
        fills = await self._fill_many(pvs, sorted_ts)
        for pv, fill in fills.items():
            self.rows.set_values(pv, sorted_ts, fill.values)
            if tail.size:
                pos = np.searchsorted(sorted_ts, tail)
                for t, p in zip(tail.tolist(), pos.tolist()):
                    self.tail_base.setdefault(t, {})[pv] = float(fill.values[p])
            self._apply_history(pv, fill, now)

    def _copy_from(self, source: TableStore, view: TableView) -> None:
        ts, vals = source.rows.ordered()
        shared = {pv: vals[pv] for pv in view.pvs if pv in vals}
        self.rows.vals = {}
        self.rows.rebuild(ts, shared, capacity=view.n)
        self.total_final = source.total_final
        self.index_hwm = source.index_hwm
        self.searched_from = source.searched_from
        self.tail_ts = list(source.tail_ts)
        self.tail_arrival = list(source.tail_arrival)
        self.tail_base = {t: dict(cells) for t, cells in source.tail_base.items()}
        # The index key holds the edge rule, so only a same-rule store is a source.
        self.edge_prev = source.edge_prev if source.view.trigger_edge == view.trigger_edge else None
        for pv in view.pvs:
            if pv in shared and pv in source.inputs:
                self.inputs[pv] = replace(source.inputs[pv])

    async def _extend_older(self, view: TableView, now: int) -> None:
        """Seed rows older than anything searched so far, down to N rows or the lookback."""
        need = view.n - self.rows.count - len(self.tail_ts)
        if need <= 0:
            return
        lo = now - view.lookback_ns
        oldest = self.rows.oldest_ts
        fresh = self.searched_from is None and oldest is None
        bound = self.searched_from if self.searched_from is not None else oldest
        if view.trigger_pv is not None:
            hi = (bound - 1) if bound is not None else now
            if hi < lo:
                return
            row_ts, arrival, trig_history, failed = await self._trigger_rows(view.trigger_pv, lo, hi, need)
            st = self.inputs[view.trigger_pv]
            if fresh or trig_history != "seeded" or st.history == "live-only":
                st.history = trig_history
            # Only a failed fetch is retried; an archive with no trigger samples is not.
            st.retry_at = now + self.settings.retry_ns if failed else None
            if fresh and view.trigger_edge is None:
                # No rows yet: live trigger samples from the lookback on may still become rows.
                self.index_hwm = int(row_ts[-1]) if row_ts.size else lo - 1
            elif fresh:
                # An edge index resumes after the newest *sample* the seed saw, with its
                # predecessor -- never behind what the live path already consumed.
                seen = self._seed_seen
                hwm = seen[0] if seen is not None else lo - 1
                if self.index_hwm is None or hwm >= self.index_hwm:
                    self.index_hwm = hwm
                    if seen is not None:
                        self.edge_prev = seen[1]
        else:
            failed = False
            dt = view.grid_dt_ns
            hi_g = (-(-bound // dt) * dt - dt) if bound is not None else (now // dt) * dt
            lo_g = max(lo, hi_g - (need - 1) * dt)
            first = -(-lo_g // dt) * dt
            row_ts = np.arange(first, hi_g + 1, dt, dtype=np.int64) if hi_g >= first else np.empty(0, np.int64)
            arrival = row_ts.copy()
            if fresh:
                self.index_hwm = hi_g
        # Everything from ``lo`` up is now known, unless N rows were found first --
        # or the trigger's archive failed transiently (its retry searches again).
        if view.trigger_pv is None or not failed or row_ts.size:
            found_all = row_ts.size >= need
            self.searched_from = int(row_ts[0]) if found_all and row_ts.size else lo
        if not row_ts.size:
            return
        fills = await self._fill_many(list(view.pvs), row_ts)
        for pv, fill in fills.items():
            if pv == view.trigger_pv:  # its history comes from the index fetch above
                self.inputs[pv].has_sample |= fill.has_sample
                continue
            self._apply_history(pv, fill, now)
        vals = {pv: f.values for pv, f in fills.items()}
        cols = compute_columns(view, vals, len(row_ts))
        keep = _row_has_value(cols, len(row_ts))
        if fresh:
            keep &= self._start_tail(row_ts, arrival, vals, now)
        self._prepend_final(row_ts[keep], {pv: v[keep] for pv, v in vals.items()})

    def _start_tail(self, row_ts: np.ndarray, arrival: np.ndarray, vals: dict[str, np.ndarray], now: int) -> np.ndarray:
        """Make the newest seeded rows still within the settle horizon the tail; returns the final-row mask."""
        wm = self._watermark()
        prov = (arrival + self.settings.settle_ns > wm) & (arrival + 2 * self.settings.settle_ns > now)
        final = ~prov
        # Tail rows must be the newest ones: everything after the first provisional row.
        if prov.any():
            first_prov = int(np.argmax(prov))
            final[first_prov:] = False
        self.tail_ts = [int(t) for t in row_ts[~final]]
        self.tail_arrival = [int(a) for a in arrival[~final]]
        prov_idx = np.flatnonzero(~final)
        self.tail_base = {int(row_ts[i]): {pv: float(v[i]) for pv, v in vals.items()} for i in prov_idx}
        return final

    def _prepend_final(self, ts: np.ndarray, vals: dict[str, np.ndarray]) -> None:
        """Insert finalized rows older than every held row in front of them."""
        held_ts, held_vals = self.rows.ordered()
        new_vals = {
            pv: np.concatenate(
                [
                    vals[pv] if pv in vals else np.full(len(ts), np.nan),
                    held_vals.get(pv, np.full(len(held_ts), np.nan)),
                ]
            )
            for pv in set(vals) | set(held_vals)
        }
        self.rows.rebuild(np.concatenate([ts, held_ts]), new_vals)
        self.total_final += len(ts)

    def _apply_history(self, pv: str, fill: _Fill, now: int) -> None:
        st = self.inputs[pv]
        st.history = fill.history
        st.has_sample = st.has_sample or fill.has_sample
        st.retry_at = now + self.settings.retry_ns if fill.history == "live-only" else None

    # --- trigger index seed --------------------------------------------------- #

    async def _trigger_rows(self, pv: str, lo: int, hi: int, need: int) -> tuple[np.ndarray, np.ndarray, History, bool]:
        """Newest ``need`` trigger samples in ``[lo, hi]``: the ring first, then the archive, newest-first.

        Returns the row times, their arrivals, the trigger's history and whether
        the archive fetch failed transiently (the only case that is retried).
        """
        edge = self.view.trigger_edge
        if edge is not None:
            row_ts, history, ring_covers = await self._edge_trigger_rows(pv, edge, lo, hi, need)
        else:
            sl = await _ring_read(self.port.ring_slice, pv, lo)
            in_range = sl.ts_ns <= hi
            (r_ts,) = _coalesce(sl.ts_ns[in_range])
            ring_covers = self.port.last_at_or_before(pv, lo - 1) is not None
            history = "seeded"
            a_ts = np.empty(0, dtype=np.int64)
            if r_ts.size < need and not ring_covers:
                a_hi = int(r_ts[0]) - 1 if r_ts.size else hi
                fetched, history = await _from_archive(
                    self._archive_trigger(pv, lo, a_hi, need - r_ts.size), "table trigger seed", pv
                )
                if fetched is not None:
                    a_ts = fetched
            row_ts = np.concatenate([a_ts, r_ts])[-need:] if need > 0 else np.empty(0, np.int64)
        # Only a transient archive failure leaves "live-only" here.
        failed = history == "live-only"
        # Seeded rows arrive at their own time: they are history, final unless within H of now.
        arrival = row_ts.copy()
        if history == "seeded":
            if row_ts.size == 0:
                history = "live-only"
            elif row_ts.size < need and not ring_covers:
                history = "truncated"
        return row_ts.astype(np.int64), arrival.astype(np.int64), history, failed

    async def _edge_trigger_rows(
        self, pv: str, edge: str, lo: int, hi: int, need: int
    ) -> tuple[np.ndarray, History, bool]:
        """Edge rows in ``[lo, hi]`` (A19): edges decided over the raw archive + ring samples, then coalesced.

        Records the newest sample seen and its predecessor in ``_seed_seen``.
        """
        sl = await _ring_read(self.port.ring_slice, pv, lo)
        in_range = sl.ts_ns <= hi
        r_ts, r_vals = sl.ts_ns[in_range], sl.values[in_range]
        ring_before = self.port.last_at_or_before(pv, lo - 1)
        ring_covers = ring_before is not None
        first_prev: float | None = None
        # The predecessor is the newest NUMERIC sample before ``lo``: walk back over
        # non-numeric ring samples (bounded; past that the oldest sample never fires).
        rec = ring_before
        for _ in range(_PREV_WALK):
            if rec is None:
                break
            _, first_prev = _edge_mask(_object_array([rec.value]), edge, None)
            if first_prev is not None:
                break
            rec = self.port.last_at_or_before(pv, rec.ts_ns - 1)
        fire, _ = _edge_mask(r_vals, edge, first_prev)
        (r_edges,) = _coalesce(r_ts[fire])
        history: History = "seeded"
        a_ts, a_vals = np.empty(0, dtype=np.int64), np.empty(0, dtype=object)
        if r_edges.size < need and not ring_covers:
            # The archive's newest sample is the predecessor of the ring's oldest.
            a_hi = int(r_ts[0]) - 1 if r_ts.size else hi
            block, history = await _from_archive(
                self._archive_trigger_raw(pv, lo, a_hi, need - r_edges.size), "table trigger seed", pv
            )
            if block is not None:
                keep = np.asarray(block.ts_ns) <= a_hi
                a_ts, a_vals = np.asarray(block.ts_ns, dtype=np.int64)[keep], block.values[keep]
        all_ts = np.concatenate([a_ts, r_ts]).astype(np.int64)
        all_vals = np.concatenate([a_vals, r_vals]) if a_ts.size else r_vals
        # Samples before ``lo`` (an archive block's leading prior sample) are predecessors only;
        # ``first_prev`` is set only where the ring covers ``lo``, when no archive is read.
        fire, prev = _edge_mask(all_vals, edge, first_prev)
        (edges,) = _coalesce(all_ts[fire & (all_ts >= lo)])
        if all_ts.size:
            self._seed_seen = (int(all_ts[-1]), prev)
        elif ring_before is not None:
            self._seed_seen = (int(ring_before.ts_ns), first_prev)
        else:
            self._seed_seen = None
        row_ts = edges[-need:] if need > 0 else np.empty(0, np.int64)
        return row_ts.astype(np.int64), history, ring_covers

    async def _archive_trigger_raw(self, pv: str, lo: int, hi: int, need: int) -> ArchiveBlock:
        """The raw archive trigger samples up to ``hi`` back to ``lo``, newest-first in windows."""
        await self._archive_trigger(pv, lo, hi, need, self.view.trigger_edge)
        return self._cache[pv]

    async def _archive_trigger(self, pv: str, lo: int, hi: int, need: int, edge: str | None = None) -> np.ndarray:
        """Archive trigger samples in ``[lo, hi]``, newest-first in windows until ``need`` rows.

        With an edge rule a row is an edge, not a sample: the walk goes on until the
        fetched samples hold ``need`` edge rows or it reaches ``lo`` (A19).
        """
        fetch = self.port.fetch_window_budgeted
        if fetch is None:
            raise ArchiverNotConfigured("no archiver configured")
        faithful = await self._faithful()
        blocks: list[ArchiveBlock] = []
        width = FIRST_SEED_WINDOW_NS
        cur_hi = hi
        found = 0
        while cur_hi >= lo:
            a = max(lo, cur_hi - width)
            block = await fetch(pv, a, cur_hi)
            blocks.append(block)
            if edge is not None:
                found = _edge_rows(ArchiveBlock.concat(blocks), edge, lo, hi)
            elif not faithful:
                found = len(_changes(ArchiveBlock.concat(blocks), lo))
            else:
                found += int(np.unique(np.asarray(block.ts_ns)[np.asarray(block.ts_ns) >= a] // FLUSH_BUCKET_NS).size)
            if found >= need or a == lo:
                break
            if edge is not None:
                # Rows per ns over everything walked so far (edges can be far sparser than samples).
                rate = max(found, 1) / max(hi - a, 1)
            else:
                rate = max(len(block), 1) / max(cur_hi - a, 1)
            width = int(min(max((need - found) / rate * 1.2, width), width * 8))
            cur_hi = a - 1
        merged = ArchiveBlock.concat(blocks)
        self._cache[pv] = merged
        if not faithful:
            return _changes(merged, lo)
        ts = np.asarray(merged.ts_ns)
        ts = ts[(ts >= lo) & (ts <= hi)]
        (ts,) = _coalesce(ts)
        return ts

    async def _faithful(self) -> bool:
        if self._event_faithful is None:
            fn = self.port.event_faithful
            try:
                self._event_faithful = True if fn is None else bool(await fn())
            except Exception:
                self._event_faithful = True
        return self._event_faithful

    # --- cell fill --------------------------------------------------------- #

    async def _fill_many(self, pvs: list[str], row_ts: np.ndarray) -> dict[str, _Fill]:
        results = await asyncio.gather(*(self._fill_input(pv, row_ts) for pv in pvs))
        return dict(zip(pvs, results))

    def _ring_view(self, pv: str, first: int) -> tuple[SampleRecord | None, RingSlice]:
        return self.port.last_at_or_before(pv, first), self.port.ring_slice(pv, first)

    async def _fill_input(self, pv: str, row_ts: np.ndarray) -> _Fill:
        """Cells of ``pv`` at ``row_ts``: the newer of the ring's and the archive's value (b6:CF-2)."""
        n = len(row_ts)
        if n == 0:
            return _Fill(np.empty(0), "seeded", False)
        first = int(row_ts[0])
        # Where the ring reaches back: rows older than its first sample need the archive.
        before, sl = await _ring_read(self._ring_view, pv, first)
        ring_from = first if before is not None else (int(sl.ts_ns[0]) if sl.ts_ns.size else None)
        history: History = "seeded"
        block: ArchiveBlock | None = None
        if ring_from is None or ring_from > first:
            a_rows = row_ts if ring_from is None else row_ts[row_ts < ring_from]
            block, history = await _from_archive(self._archive_cells(pv, a_rows), "table seed", pv)
            # Re-read the ring after the fetch, so a sample (or Hare initial) that
            # landed meanwhile counts -- one O(k) copy, applied on the loop.
            before, sl = await _ring_read(self._ring_view, pv, first)
        faithful = await self._faithful() if block is not None else True

        def compute() -> tuple[np.ndarray, bool]:
            return _merge_cells(row_ts, sl, before, block, faithful)

        size = len(sl.ts_ns) + (len(block) if block is not None else 0)
        if size > _THREAD_FILL_THRESHOLD:
            async with _seed_cpu():
                values, has = await asyncio.to_thread(compute)
        else:
            values, has = compute()
        return _Fill(values, history, has)

    async def _archive_cells(self, pv: str, a_rows: np.ndarray) -> ArchiveBlock | list:
        """Archive samples covering ``a_rows``: a windowed fetch, or last-before lookups when sparse."""
        cached = self._cache.get(pv)
        if cached is not None and len(cached) and int(cached.ts_ns[0]) <= int(a_rows[0]):
            return cached
        fetch = self.port.fetch_window_budgeted
        if fetch is None:
            raise ArchiverNotConfigured("no archiver configured")
        t0, t1 = int(a_rows[0]), int(a_rows[-1])
        rows = len(a_rows)
        if rows > LAST_BEFORE_MAX_TIMESTAMPS or self.port.fetch_last_before is None or t1 - t0 <= FIRST_SEED_WINDOW_NS:
            return await fetch(pv, t0, t1)
        # Probe the newest window, then decide from the observed rate (b4:CC-2, b7:CC-2).
        p_lo = max(t0, t1 - FIRST_SEED_WINDOW_NS)
        probe = await fetch(pv, p_lo, t1)
        rate = len(probe) / max(t1 - p_lo, 1)
        requests = math.ceil(rate * (p_lo - t0) / SEED_SAMPLE_BUDGET)
        if requests <= rows / 20:
            older = await fetch(pv, t0, p_lo - 1)
            return ArchiveBlock.concat([older, probe])
        # Sparse rows over a dense PV: one lookup per row the probe does not cover.
        covered = np.searchsorted(np.asarray(probe.ts_ns), a_rows, side="right") > 0
        lookup = [int(t) for t in a_rows[~covered]]
        samples = await self.port.fetch_last_before(pv, lookup)
        extra = ArchiveBlock.from_samples([s for s in samples if s is not None])
        return ArchiveBlock.concat([extra, probe])

    # --- tick ---------------------------------------------------------------- #

    def _watermark(self) -> int:
        fn = self.port.watermark
        return int(fn()) if fn is not None else 0

    def _latest_ts(self, pv: str) -> int | None:
        rec = self.port.last_at_or_before(pv, _FAR_FUTURE)
        return rec.ts_ns if rec is not None else None

    def tick(self, now: int | None = None) -> bool:
        """Advance the index, finalize settled rows, recompute the tail. True if the content changed."""
        view = self.view
        now = self.port.now_ns() if now is None else now
        wm = self._watermark()
        version_before = self.version

        trig_recs = self._scan_live(view, self.rows.newest_ts)
        new_rows = self._advance_index(view, now, trig_recs)

        # Pending = old tail + new rows; recompute every cell from the rings.
        p_ts = self.tail_ts + [t for t, _ in new_rows]
        p_arr = self.tail_arrival + [a for _, a in new_rows]
        k = len(p_ts)
        p_ts_arr = np.asarray(p_ts, dtype=np.int64)
        base = self.tail_base
        cells = self._pending_cells(view, p_ts)
        cols = compute_columns(view, cells, k)
        nonnull = _row_has_value(cols, k)

        # Finalize the settled prefix.
        n_final = max(self._settled_prefix(view, p_ts, p_arr, now, wm), k - view.n)  # the tail is capped at N
        final_seq: list[tuple[int, tuple]] = []
        for i in range(n_final):
            if not nonnull[i]:
                continue
            row = {pv: float(cells[pv][i]) for pv in view.pvs}
            self.rows.append(p_ts[i], row)
            self.total_final += 1
            final_seq.append((p_ts[i], tuple(row[pv] for pv in view.pvs)))
        self.tail_ts = p_ts[n_final:]
        self.tail_arrival = p_arr[n_final:]
        if base:
            live = set(self.tail_ts)
            self.tail_base = {t: c for t, c in base.items() if t in live}
        vis = nonnull[n_final:]
        vis_ts = p_ts_arr[n_final:][vis]
        vis_vals = {pv: cells[pv][n_final:][vis] for pv in view.pvs}
        new_seq = final_seq + [
            (int(vis_ts[j]), tuple(float(vis_vals[pv][j]) for pv in view.pvs)) for j in range(len(vis_ts))
        ]
        old_seq = [
            (
                int(self._tail_vis_ts[j]),
                tuple(float(self._tail_vis_vals[pv][j]) if pv in self._tail_vis_vals else math.nan for pv in view.pvs),
            )
            for j in range(len(self._tail_vis_ts))
        ]
        if not _seq_equal(old_seq, new_seq):
            self.version += 1
        self._tail_vis_ts = vis_ts
        self._tail_vis_vals = vis_vals

        sig = self._input_signature(now)
        if sig != self._inputs_sig:
            self._inputs_sig = sig
            self.version += 1
        return self.version != version_before

    def _pending_cells(self, view: TableView, p_ts: list[int]) -> dict[str, np.ndarray]:
        """Every input's cell at each pending row, from the ring; the seeded tail cell where the ring has none."""
        cells: dict[str, np.ndarray] = {}
        base = self.tail_base
        for pv in view.pvs:
            col = np.empty(len(p_ts), dtype=np.float64)
            for i, t in enumerate(p_ts):
                rec = self.port.last_at_or_before(pv, t)
                col[i] = cell_from_record(rec) if rec is not None else base.get(t, {}).get(pv, math.nan)
            cells[pv] = col
        return cells

    def _settled_prefix(self, view: TableView, p_ts: list[int], p_arr: list[int], now: int, wm: int) -> int:
        """How many leading pending rows are final: settled by watermark or clock, or every input moved past."""
        settle = self.settings.settle_ns
        latest = {pv: self._latest_ts(pv) for pv in view.pvs}
        # Edge rows (A19): a later edge in the same flush bucket replaces the row, so
        # moving past it does not finalize a row while the newest trigger sample
        # consumed is still in its bucket -- live then coalesces as a seed does.
        open_bucket = (
            self.index_hwm // FLUSH_BUCKET_NS if view.trigger_edge is not None and self.index_hwm is not None else None
        )
        n = 0
        for t, a in zip(p_ts, p_arr):
            moved_past = all(latest[pv] is not None and latest[pv] > t for pv in view.pvs)
            if open_bucket is not None and t // FLUSH_BUCKET_NS == open_bucket:
                moved_past = False
            if not (wm >= a + settle or now >= a + 2 * settle or moved_past):
                break
            n += 1
        return n

    def _scan_live(self, view: TableView, pre_final_ts: int | None) -> list[SampleRecord]:
        """Update every input's late-data state; returns the trigger's new samples (one samples_since per input)."""
        trig_recs: list[SampleRecord] = []
        for pv in view.pvs:
            st = self.inputs[pv]
            recs = self.port.samples_since(pv, st.last_seen_recv)
            if not recs:
                continue
            st.last_seen_recv = max(st.last_seen_recv, max(r.recv_ns for r in recs))
            for r in recs:
                if r.after_connect:
                    continue
                late = pre_final_ts is not None and r.ts_ns <= pre_final_ts
                skew = abs(r.recv_ns - r.ts_ns) > self.settings.settle_ns
                if late or skew:
                    st.last_late_ns = r.recv_ns if st.last_late_ns is None else max(st.last_late_ns, r.recv_ns)
            if pv == view.trigger_pv:
                trig_recs = recs
        return trig_recs

    def _advance_index(self, view: TableView, now: int, trig_recs: list[SampleRecord]) -> list[tuple[int, int]]:
        """New index points as ``(row ts, arrival)``, at most N; moves ``index_hwm`` past them."""
        hwm = self.index_hwm
        if view.trigger_pv is not None and view.trigger_edge is not None:
            new_rows = self._advance_edges(view, trig_recs)
            self.backfill_pending = False
            return new_rows[-view.n :]
        if view.trigger_pv is not None:
            if self.backfill_pending:
                sl = self.port.ring_slice(view.trigger_pv, (hwm + 1) if hwm is not None else 0)
                (ts_b,) = _coalesce(sl.ts_ns)
                recv_map = {r.ts_ns: r.recv_ns for r in trig_recs}
                cand = [(int(t), recv_map.get(int(t), 0) or int(t)) for t in ts_b]
                seen = {t for t, _ in cand}
                cand += [(r.ts_ns, r.recv_ns) for r in trig_recs if r.ts_ns not in seen]
                cand.sort()
            else:
                cand = [(r.ts_ns, r.recv_ns) for r in trig_recs]
            new_rows = [(t, a) for t, a in cand if hwm is None or t > hwm]
        else:
            dt = view.grid_dt_ns
            top = (now // dt) * dt
            start = (hwm + dt) if hwm is not None else top
            if top >= start:
                count = (top - start) // dt + 1
                first = start + max(0, count - view.n) * dt
                new_rows = [(int(g), int(g)) for g in range(first, top + 1, dt)]
            else:
                new_rows = []
        self.backfill_pending = False
        if len(new_rows) > view.n:  # one tick appends at most N rows (b5:CF-1)
            new_rows = new_rows[-view.n :]
        if new_rows:
            self.index_hwm = new_rows[-1][0]
        return new_rows

    def _advance_edges(self, view: TableView, trig_recs: list[SampleRecord]) -> list[tuple[int, int]]:
        """New edge rows as ``(row ts, arrival)`` from the trigger samples after ``index_hwm`` (A19).

        Edges are decided over the raw samples (the ring slice on a backfill, the
        raw live records otherwise) against the persisted predecessor, then only
        the edge times are coalesced to the last per flush bucket. An edge in the
        bucket of the newest tail row replaces that row, so a bucket split over
        two ticks gives the row a seed would.
        """
        hwm = self.index_hwm
        samples: list[tuple[int, int, Any]] = []
        if self.backfill_pending:
            sl = self.port.ring_slice(view.trigger_pv, (hwm + 1) if hwm is not None else 0)
            recv_map = {r.ts_ns: r.recv_ns for r in trig_recs}
            samples = [(int(t), recv_map.get(int(t), 0) or int(t), v) for t, v in zip(sl.ts_ns.tolist(), sl.values)]
            seen = {t for t, _, _ in samples}
            samples += [(r.ts_ns, r.recv_ns, r.value) for r in trig_recs if r.ts_ns not in seen]
        else:
            samples = [(r.ts_ns, r.recv_ns, r.value) for r in trig_recs]
        samples = sorted((s for s in samples if hwm is None or s[0] > hwm), key=lambda s: s[0])
        if not samples:
            return []
        ts = np.fromiter((s[0] for s in samples), dtype=np.int64, count=len(samples))
        arrival = np.fromiter((s[1] for s in samples), dtype=np.int64, count=len(samples))
        fire, self.edge_prev = _edge_mask(_object_array([s[2] for s in samples]), view.trigger_edge, self.edge_prev)
        self.index_hwm = int(ts[-1])
        e_ts, e_arr = _coalesce(ts[fire], arrival[fire])
        if e_ts.size and self.tail_ts:
            last = self.tail_ts[-1]
            if last < e_ts[0] and last // FLUSH_BUCKET_NS == int(e_ts[0]) // FLUSH_BUCKET_NS:
                self.tail_ts = self.tail_ts[:-1]
                self.tail_arrival = self.tail_arrival[:-1]
                self.tail_base.pop(last, None)
        return [(int(t), int(a)) for t, a in zip(e_ts.tolist(), e_arr.tolist())]

    def _newest_row_ts(self) -> int | None:
        if len(self._tail_vis_ts):
            return int(self._tail_vis_ts[-1])
        return self.rows.newest_ts

    def input_state(self, pv: str, now: int) -> tuple[str, str, bool]:
        st = self.inputs[pv]
        newest = self._newest_row_ts()
        probe = newest if newest is not None else now
        has = st.has_sample or self.port.last_at_or_before(pv, probe) is not None
        late = st.last_late_ns is not None and now - st.last_late_ns <= self.settings.late_window_ns
        return st.history, ("ok" if has else "no-data"), late

    def _input_signature(self, now: int) -> tuple:
        return tuple(self.input_state(pv, now) for pv in self.view.pvs)

    def due_retries(self, now: int) -> list[str]:
        return [
            pv
            for pv in self.view.pvs
            if self.inputs[pv].history == "live-only"
            and self.inputs[pv].retry_at is not None
            and now >= self.inputs[pv].retry_at
        ]

    def start_retry(self, pvs: list[str]) -> asyncio.Task:
        for pv in pvs:
            self.inputs[pv].retry_at = None
        return self.chain(lambda: self._retry(pvs))

    async def _retry(self, pvs: list[str]) -> None:
        """Re-seed live-only inputs and patch the rows already held (b3 patch path)."""
        view = self.view
        now = self.port.now_ns()
        self._cache = {}
        trig = view.trigger_pv
        others = [pv for pv in pvs if pv != trig]
        if trig in pvs:
            await self._extend_older(view, now)
        if others:
            await self._fill_held(others, now)
        self._cache = {}
        self.version += 1

    # --- frame ---------------------------------------------------------------- #

    def frame(self, now: int | None = None) -> TableFrame:
        """A copy of the newest N rows of finalized ∪ tail, safe to hand to the encode thread."""
        now = self.port.now_ns() if now is None else now
        view = self.view
        k_tail = len(self._tail_vis_ts)
        k_final = max(0, min(self.rows.count, view.n - k_tail))
        ts_f, vals_f = self.rows.newest(k_final)
        if k_tail:
            ts = np.concatenate([ts_f, self._tail_vis_ts])
            inputs = {
                pv: np.concatenate([vals_f.get(pv, np.full(k_final, np.nan)), self._tail_vis_vals[pv]])
                for pv in view.pvs
            }
        else:
            ts = ts_f
            inputs = {pv: vals_f.get(pv, np.full(k_final, np.nan)) for pv in view.pvs}
        columns = compute_columns(view, inputs, len(ts))
        states = {binding: self.input_state(pv, now) for binding, pv in view.bindings.items()}
        return TableFrame(ts_ns=ts, columns=columns, inputs=states, total_rows=self.total_final + k_tail)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _from_archive(fetch: Awaitable[Any], what: str, pv: str) -> tuple[Any, History]:
    """Await an archive read: ``(result, "seeded")``, else ``(None, history)`` by failure.

    An archive without the PV (or none at all) is ``not-archived``; any other
    failure is transient, ``live-only`` and retried later.
    """
    try:
        return await fetch, "seeded"
    except (ArchiverNotConfigured, PVNotFoundInArchiver):
        return None, "not-archived"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("%s for %s failed: %s", what, pv, exc)
        return None, "live-only"


def _row_has_value(cols: dict[str, np.ndarray], k: int) -> np.ndarray:
    if k == 0 or not cols:
        return np.zeros(k, dtype=bool)
    stacked = np.vstack([np.asarray(c, dtype=np.float64) for c in cols.values()])
    return ~np.isnan(stacked).all(axis=0)


def _seq_equal(a: list[tuple[int, tuple]], b: list[tuple[int, tuple]]) -> bool:
    if len(a) != len(b):
        return False
    for (ta, va), (tb, vb) in zip(a, b):
        if ta != tb or len(va) != len(vb):
            return False
        for x, y in zip(va, vb):
            if not (x == y or (math.isnan(x) and math.isnan(y))):
                return False
    return True


def _edge_rows(block: ArchiveBlock, edge: str, lo: int, hi: int) -> int:
    """How many ``edge`` rows the samples of ``block`` give in ``[lo, hi]`` (A19).

    The oldest sample has no predecessor here and never fires, so this never
    over-counts what the seed decides with the full sample run.
    """
    ts = np.asarray(block.ts_ns)
    if ts.size == 0:
        return 0
    fire, _ = _edge_mask(block.values, edge, None)
    (edges,) = _coalesce(ts[fire & (ts >= lo) & (ts <= hi)].astype(np.int64))
    return int(edges.size)


def _changes(block: ArchiveBlock, lo: int) -> np.ndarray:
    """A polled archive's trigger rows: documents whose value differs from the previous one (b6:IA-4)."""
    ts = np.asarray(block.ts_ns)
    if ts.size == 0:
        return np.empty(0, dtype=np.int64)
    vals = block.values
    keep = np.zeros(ts.size, dtype=bool)
    for i in range(1, ts.size):
        keep[i] = not _same(vals[i], vals[i - 1])
    keep &= ts >= lo
    return ts[keep].astype(np.int64)


def _same(a: Any, b: Any) -> bool:
    try:
        return bool(a == b)
    except Exception:
        return a is b


def _merge_cells(
    row_ts: np.ndarray,
    sl: RingSlice,
    before: SampleRecord | None,
    block: ArchiveBlock | None,
    faithful: bool,
) -> tuple[np.ndarray, bool]:
    """Vectorised seed fill: per row, the newer of ring and archive sample at or before it."""
    n = len(row_ts)
    # Ring side, with the sample before the slice prepended.
    r_ts = np.asarray(sl.ts_ns, dtype=np.int64)
    r_vals = numeric_array(sl.values)
    r_bad = np.asarray(sl.severities) == _INVALID
    if before is not None:
        r_ts = np.concatenate([[before.ts_ns], r_ts])
        r_vals = np.concatenate([[_numeric(before.value)], r_vals])
        r_bad = np.concatenate([[before.severity is AlarmSeverity.INVALID or before.in_outage], r_bad])
    r_idx = np.searchsorted(r_ts, row_ts, side="right") - 1
    r_has = r_idx >= 0
    r_sample_ts = np.where(r_has, r_ts[np.maximum(r_idx, 0)] if r_ts.size else 0, np.iinfo(np.int64).min)
    out = np.full(n, np.nan)
    if r_ts.size:
        ri = np.maximum(r_idx, 0)
        ring_cell = np.where(r_bad[ri], np.nan, r_vals[ri])
        ring_cell[_outage_mask(row_ts, sl.outages)] = np.nan
        out = np.where(r_has, ring_cell, np.nan)
    has = bool(r_has.any())
    if block is not None and len(block):
        a_ts = np.asarray(block.ts_ns, dtype=np.int64)
        a_vals = numeric_array(block.values)
        if faithful:
            a_vals = np.where(np.asarray(block.severities) == _INVALID, np.nan, a_vals)
        a_idx = np.searchsorted(a_ts, row_ts, side="right") - 1
        a_has = a_idx >= 0
        a_sample_ts = np.where(a_has, a_ts[np.maximum(a_idx, 0)], np.iinfo(np.int64).min)
        use_archive = a_has & (a_sample_ts > r_sample_ts)
        out = np.where(use_archive, a_vals[np.maximum(a_idx, 0)], out)
        has = has or bool(a_has.any())
    return out, has
