"""The workspace hub: the one in-memory slot through which an agent changes the CANOPY workspace.

One process-wide :class:`Hub` (``tools._common.get_hub()``) holds what the active window shows
and the batches the agent sent it. It is ephemeral: a restart loses pending batches, and the
window that held the slot claims it again. It does no I/O; the HTTP routes and the MCP tools
are thin shells over it, and its clock is injectable so every timing rule can be replayed.

The slot (protocol 1):

- **Claim always wins.** :meth:`Hub.claim` makes a window the active one, ends the previous
  window's stream with ``inactive`` and resolves the previous window's unresolved batches
  ``window_gone``. With ``if_vacant`` it succeeds only while no active window is connected.
- **Only the slot holder reports.** :meth:`Hub.report` from any other window is ``not_active``
  and never claims. A report whose ``seq`` is not above the slot's ``seq`` (the active window's
  high-water mark, set by its claim) changes nothing.
- **Every state is normalised** before it is kept: a closed schema, canonical tab keys (screen
  ``<category>/<id>`` plus canonical macros, at most 16, values at most 64 printable ASCII
  characters without whitespace; tools ``@<tool>`` / ``@<tool>/<n>``), queries of at most 4096
  bytes of printable ASCII, labels of at most 120 characters with control and bidi characters
  stripped. Anything else is :class:`WorkspaceError` ``invalid_state``.

Batches:

- :meth:`Hub.enqueue` checks shape only (verb enum, argument names, types and enums, 1 to 20
  commands, macro and query bounds) and refuses ``invalid_command``; a ``batch_id`` this epoch
  minted returns that batch and enqueues nothing, any other is ``unknown_batch``; then
  ``no_window`` (no active window, or its stream gone for 3 s), ``stale_state`` (``base.window``
  is not the active window; ``base.seq`` is the viewer's to check), ``window_hidden`` and
  ``queue_full`` (more than 32 unresolved commands for the window). A refused batch has id
  ``None`` and no results.
- An accepted batch is bound to the active window for good and gets ids ``b-<epoch>-<n>`` and
  ``c-<epoch>-<n>``. A window holds at most one delivered, unresolved batch; the next one is
  delivered on its stream when that one resolves. A batch not delivered by enqueue + 10 s is
  ``expired``; a delivered one without a result by delivery + 10 s is ``lost``; a window whose
  stream has been gone 3 s, or that lost the slot, leaves its batches ``window_gone``.
- :meth:`Hub.post_result` takes the one result per batch from the window that holds it
  (``not_claimed`` otherwise, ``batch_closed`` once resolved). Results are always stored (the
  last 100 commands); the posted state is written only from the active window with a higher
  ``seq``.
- :meth:`Hub.wait` lets a tool wait for a batch; :meth:`Hub.close` wakes every waiter at once
  (they see ``pending``) and ends every stream.

Keys, queries and labels are display data for the agent, never instructions.
"""

from __future__ import annotations

import re
import copy
import json
import math
import time
import asyncio
import secrets
from typing import Any
from datetime import UTC, datetime
from collections import OrderedDict, deque
from urllib.parse import unquote
from collections.abc import Mapping, Callable

from canopy_mcp.store import ID_PATTERN
from canopy_mcp.member_key import canonical_macros

PROTOCOL = 1

MAX_COMMANDS = 20
MAX_PENDING = 32
MAX_RESULTS = 100
MAX_BATCH_IDS = 256
MAX_TABS = 50
MAX_MACROS = 16
MAX_MACRO_VALUE = 64
MAX_QUERY_BYTES = 4096
MAX_LABEL = 120
MAX_LAYOUT_DEPTH = 6
MAX_LAYOUT_BYTES = 32 * 1024
MAX_VIEW_BYTES = 4096
MAX_WINDOW_ID = 128
MIN_PERCENT, MAX_PERCENT = 10, 400

DELIVERY_MS = 10_000
"""An undelivered batch expires at enqueue + this; a delivered one is lost at delivery + this."""
GONE_MS = 3_000
"""A window whose stream has been gone this long (or never opened since its claim) is gone."""
MAX_WAIT_S = 10.0

TOOLS = ("trace", "correlation", "waveform")
SCREEN_CATEGORIES = ("library", "drafts")
EDGES = ("left", "right", "above", "below")
MODES = ("polite", "append", "replace")
STEPS = ("in", "out", "fit", "actual")

# verb -> (required argument names, optional argument names)
VERBS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "open": (frozenset({"ref"}), frozenset({"macros", "mode", "key"})),
    "focus": (frozenset({"key"}), frozenset()),
    "close": (frozenset({"key"}), frozenset()),
    "close_others": (frozenset({"key"}), frozenset()),
    "close_right": (frozenset({"key"}), frozenset()),
    "close_all": (frozenset(), frozenset()),
    "reopen": (frozenset(), frozenset({"id"})),
    "back": (frozenset({"key"}), frozenset()),
    "forward": (frozenset({"key"}), frozenset()),
    "place": (frozenset({"key"}), frozenset({"edge", "beside"})),
    "remove_tile": (frozenset({"key"}), frozenset()),
    "reset_layout": (frozenset(), frozenset()),
    "zoom": (frozenset({"key"}), frozenset({"step", "percent"})),
    "open_collection": (frozenset({"ref"}), frozenset({"layout"})),
    "open_tool": (frozenset({"tool"}), frozenset({"query"})),
    "set_query": (frozenset({"key", "query"}), frozenset()),
}
_ENUMS = {"mode": MODES, "edge": EDGES, "step": STEPS, "tool": TOOLS}

REFUSAL_REASONS = ("no_window", "window_hidden", "queue_full", "invalid_command", "stale_state", "unknown_batch")
POSTED_STATUSES = ("applied", "rejected", "skipped")
# What a posted command result may carry besides id and status, and the types it may have.
_RESULT_EXTRAS: dict[str, tuple[type, ...]] = {
    "changed": (bool,),
    "reason": (str,),
    "after": (str,),
    "key": (str,),
    "mode": (str,),
    "group": (str,),
    "keys": (list,),
    "percent": (int, float),
    "previous_query": (str,),
}

STATE_KEYS = frozenset({"tabs", "active", "layout", "zoom", "instances", "toolMru", "labels"})
TAB_KEYS = frozenset({"key", "kind", "docked", "canBack", "canForward"})
INSTANCE_KEYS = frozenset({"tool", "n", "query", "view"})
_ROOT_KEYS = frozenset({"split", "items", "active"})
_NODE_KEYS = frozenset({"items", "size"})
_LEAF_KEYS = frozenset({"key", "size"})

_MACRO_VALUE = re.compile(rf"[\x21-\x7e]{{0,{MAX_MACRO_VALUE}}}")
_PRINTABLE = re.compile(r"[\x20-\x7e]*")
_WINDOW_ID = re.compile(rf"[\x21-\x7e]{{1,{MAX_WINDOW_ID}}}")
_TOOL_KEY = re.compile(rf"@({'|'.join(TOOLS)})(?:/([1-9][0-9]{{0,5}}))?")
# C0, DEL, C1 and the bidi formatting characters.
_STRIPPED = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


class WorkspaceError(Exception):
    """A window's request the hub refuses; ``code`` is the route's error, ``status`` its HTTP status."""

    STATUS = {"not_active": 409, "not_vacant": 409, "not_claimed": 409, "batch_closed": 409, "invalid_state": 422}

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = self.STATUS[code]

    def body(self) -> dict[str, str]:
        return {"error": self.code, **({"detail": self.detail} if self.detail else {})}


class StreamEnded(Exception):
    """The stream was replaced, lost the slot, or the hub closed; nothing more will arrive."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- command shape ------------------------------------------------------------------------


def _check_arg(name: str, value: Any) -> str | None:
    """Why argument ``name`` = ``value`` breaks the shape, or ``None``."""
    if name in _ENUMS:
        if not isinstance(value, str) or value not in _ENUMS[name]:
            return f"{name} must be one of {', '.join(_ENUMS[name])}"
        return None
    if name == "macros":
        if not isinstance(value, dict):
            return "macros must be an object"
        if len(value) > MAX_MACROS:
            return f"at most {MAX_MACROS} macros"
        for macro, text in value.items():
            if not isinstance(macro, str) or macro == "":
                return "a macro name must be a non-empty string"
            if not isinstance(text, str) or not _MACRO_VALUE.fullmatch(text):
                return (
                    f"macro {macro!r}: a value is at most {MAX_MACRO_VALUE} printable ASCII characters, no whitespace"
                )
        return None
    if name == "layout":
        return None if isinstance(value, bool) else "layout must be a boolean"
    if name == "percent":
        if not _is_int(value) or not MIN_PERCENT <= value <= MAX_PERCENT:
            return f"percent must be an integer from {MIN_PERCENT} to {MAX_PERCENT}"
        return None
    if name == "query":
        if not isinstance(value, str):
            return "query must be a string"
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            return "query must be valid Unicode text"
        return None if size <= MAX_QUERY_BYTES else f"query must be at most {MAX_QUERY_BYTES} UTF-8 bytes"
    return None if isinstance(value, str) else f"{name} must be a string"


def check_command(command: Any) -> str | None:
    """Why ``command`` breaks the hub's shape rules, or ``None`` when it is well formed."""
    if not isinstance(command, dict) or "cmd" not in command:
        return "a command is an object with cmd"
    extra = set(command) - {"cmd", "args"}
    if extra:
        return f"a command carries only cmd and args, not {', '.join(sorted(map(str, extra)))}"
    verb = command["cmd"]
    if not isinstance(verb, str) or verb not in VERBS:
        return f"cmd must be one of {', '.join(VERBS)}"
    args = command.get("args", {})
    if not isinstance(args, dict):
        return f"{verb}: args must be an object"
    required, optional = VERBS[verb]
    missing = required - set(args)
    if missing:
        return f"{verb} needs {', '.join(sorted(missing))}"
    unknown = set(args) - required - optional
    if unknown:
        return f"{verb} does not take {', '.join(sorted(map(str, unknown)))}"
    for name, value in args.items():
        problem = _check_arg(name, value)
        if problem:
            return f"{verb}: {problem}"
    if verb == "zoom" and len({"step", "percent"} & set(args)) != 1:
        return "zoom takes exactly one of step and percent"
    if verb == "open" and ("key" in args) != (args.get("mode") == "replace"):
        return "open takes key exactly when mode is replace"
    return None


def check_batch(commands: Any, base: Any = None, batch_id: Any = None) -> str | None:
    """Why the enqueue arguments break the shape rules, or ``None``."""
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
        return f"commands must be a list of 1 to {MAX_COMMANDS} commands"
    if base is not None:
        if not isinstance(base, dict) or set(base) != {"window", "seq"}:
            return "base must be {window, seq}"
        if not isinstance(base["window"], str) or not _is_int(base["seq"]):
            return "base.window must be a string and base.seq an integer"
    if batch_id is not None and not isinstance(batch_id, str):
        return "batch_id must be a string"
    for index, command in enumerate(commands):
        problem = check_command(command)
        if problem:
            return f"commands[{index}]: {problem}"
    return None


# --- state normalisation ------------------------------------------------------------------


def _invalid(detail: str) -> WorkspaceError:
    return WorkspaceError("invalid_state", detail)


def _closed(doc: Any, allowed: frozenset[str], where: str, exact: bool = True) -> Mapping[str, Any]:
    if not isinstance(doc, dict):
        raise _invalid(f"{where} must be an object")
    extra = set(doc) - allowed
    if extra:
        raise _invalid(f"{where} has unknown key(s): {', '.join(sorted(map(str, extra)))}")
    if exact and set(doc) != allowed:
        raise _invalid(f"{where} must carry {', '.join(sorted(allowed))}")
    return doc


def _printable(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str) or not _PRINTABLE.fullmatch(value) or len(value) > limit:
        raise _invalid(f"{where} must be at most {limit} bytes of printable ASCII")
    return value


def screen_ref(key: str) -> str | None:
    """The ``<category>/<id>`` of a canonical screen tab key, or ``None`` when ``key`` is not one."""
    ref, sep, query = key.partition("?")
    category, slash, local = ref.partition("/")
    if not slash or category not in SCREEN_CATEGORIES or not ID_PATTERN.fullmatch(local):
        return None
    if not sep:
        return ref
    macros: dict[str, str] = {}
    for pair in query.split("&"):
        name, eq, value = pair.partition("=")
        if not eq:
            return None
        try:
            name, value = unquote(name, errors="strict"), unquote(value, errors="strict")
        except UnicodeDecodeError:
            return None
        if name == "" or name in macros or not _MACRO_VALUE.fullmatch(value):
            return None
        macros[name] = value
    if len(macros) > MAX_MACROS:
        return None
    try:
        canonical = canonical_macros(macros)
    except ValueError:
        return None
    return ref if canonical == query else None


def tool_key(key: str) -> tuple[str, int] | None:
    """``(tool, n)`` of a tool tab key (``@trace`` is n 1, ``@trace/2`` n 2), or ``None``."""
    match = _TOOL_KEY.fullmatch(key)
    if not match:
        return None
    n = int(match.group(2)) if match.group(2) else 1
    if match.group(2) and n < 2:
        return None
    return match.group(1), n


def clean_label(label: Any, where: str) -> str:
    """``label`` with control and bidi characters stripped; at most 120 characters."""
    if not isinstance(label, str):
        raise _invalid(f"{where} must be a string")
    cleaned = _STRIPPED.sub("", label)
    if len(cleaned) > MAX_LABEL:
        raise _invalid(f"{where} must be at most {MAX_LABEL} characters")
    try:
        cleaned.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"{where} must be valid Unicode text") from None
    return cleaned


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))


def _layout_items(node: Mapping[str, Any], where: str, depth: int, keys: set[str], placed: set[str]) -> list:
    if depth > MAX_LAYOUT_DEPTH:
        raise _invalid(f"{where}: layout nests deeper than {MAX_LAYOUT_DEPTH}")
    items = node.get("items")
    if not isinstance(items, list) or len(items) < 2:
        raise _invalid(f"{where}.items must be an array of at least two items")
    out = []
    for index, item in enumerate(items):
        at = f"{where}.items[{index}]"
        if not isinstance(item, dict):
            raise _invalid(f"{at} must be an object")
        if "key" in item:
            _closed(item, _LEAF_KEYS, at, exact=False)
            key = item["key"]
            if not isinstance(key, str) or key not in keys:
                raise _invalid(f"{at}.key must name an open tab")
            if key in placed:
                raise _invalid(f"{at}.key {key!r} is placed twice")
            placed.add(key)
            leaf: dict[str, Any] = {"key": key}
        elif "items" in item:
            _closed(item, _NODE_KEYS, at, exact=False)
            leaf = {"items": _layout_items(item, at, depth + 1, keys, placed)}
        else:
            raise _invalid(f"{at} must carry key or items")
        if "size" in item:
            size = item["size"]
            finite = _is_int(size) or (isinstance(size, float) and math.isfinite(size))
            if not finite or size <= 0:
                raise _invalid(f"{at}.size must be a positive number")
            leaf["size"] = size
        out.append(leaf)
    return out


def validate_window_layout(layout: Any, open_keys: set[str]) -> dict[str, Any] | None:
    """A window's layout (``None``, or the collection layout schema with ``{key, size?}`` leaves)."""
    if layout is None:
        return None
    root = _closed(layout, _ROOT_KEYS, "layout", exact=False)
    if root.get("split") not in ("row", "column"):
        raise _invalid("layout.split must be 'row' or 'column'")
    if _size(layout) > MAX_LAYOUT_BYTES:
        raise _invalid(f"layout exceeds {MAX_LAYOUT_BYTES} bytes")
    placed: set[str] = set()
    out: dict[str, Any] = {"split": root["split"], "items": _layout_items(root, "layout", 1, open_keys, placed)}
    if "active" in root:
        if root["active"] not in placed:
            raise _invalid("layout.active must name a placed tab")
        out["active"] = root["active"]
    return out


def normalise_state(state: Any) -> dict[str, Any]:
    """The hub's copy of a window's ``state``; :class:`WorkspaceError` ``invalid_state`` when it breaks a rule."""
    state = _closed(state, STATE_KEYS, "state")
    tabs = state["tabs"]
    if not isinstance(tabs, list) or len(tabs) > MAX_TABS:
        raise _invalid(f"state.tabs must be a list of at most {MAX_TABS} tabs")
    out_tabs, keys, screens, tools = [], [], set(), set()
    for index, tab in enumerate(tabs):
        at = f"state.tabs[{index}]"
        _closed(tab, TAB_KEYS, at)
        key = tab["key"]
        if not isinstance(key, str) or key in keys:
            raise _invalid(f"{at}.key must be a string, once")
        ref = screen_ref(key)
        if ref is not None:
            kind = "screen"
            screens.add(ref)
        elif tool_key(key) is not None:
            kind = "tool"
            tools.add(key)
        else:
            raise _invalid(f"{at}.key is not a canonical tab key")
        if tab["kind"] != kind:
            raise _invalid(f"{at}.kind must be {kind!r}")
        for flag in ("docked", "canBack", "canForward"):
            if not isinstance(tab[flag], bool):
                raise _invalid(f"{at}.{flag} must be a boolean")
        keys.append(key)
        out_tabs.append({name: tab[name] for name in ("key", "kind", "docked", "canBack", "canForward")})
    open_keys = set(keys)

    active = state["active"]
    if active is not None and active not in open_keys:
        raise _invalid("state.active must be null or an open tab")

    layout = validate_window_layout(state["layout"], {t["key"] for t in out_tabs if t["docked"]})

    zoom = state["zoom"]
    if not isinstance(zoom, dict):
        raise _invalid("state.zoom must be an object")
    out_zoom: dict[str, Any] = {}
    for ref, entry in zoom.items():
        at = f"state.zoom[{ref!r}]"
        if ref not in screens:
            raise _invalid(f"{at} must name an open screen")
        if entry == {"mode": "fit"}:
            out_zoom[ref] = {"mode": "fit"}
            continue
        _closed(entry, frozenset({"mode", "percent"}), at)
        percent = entry["percent"]
        if entry["mode"] != "manual" or not _is_int(percent) or not MIN_PERCENT <= percent <= MAX_PERCENT:
            raise _invalid(f"{at} must be fit or manual {MIN_PERCENT}..{MAX_PERCENT}")
        out_zoom[ref] = {"mode": "manual", "percent": percent}

    instances = state["instances"]
    if not isinstance(instances, dict) or set(instances) != tools:
        raise _invalid("state.instances must hold exactly the open tool tabs")
    out_instances: dict[str, Any] = {}
    for key, inst in instances.items():
        at = f"state.instances[{key!r}]"
        _closed(inst, INSTANCE_KEYS, at)
        if (inst["tool"], inst["n"]) != tool_key(key) or not _is_int(inst["n"]):
            raise _invalid(f"{at} must name the tool and number of its key")
        query = _printable(inst["query"], f"{at}.query", MAX_QUERY_BYTES)
        view = inst["view"]
        if view is not None and (not isinstance(view, dict) or _size(view) > MAX_VIEW_BYTES):
            raise _invalid(f"{at}.view must be null or an object of at most {MAX_VIEW_BYTES} bytes")
        out_instances[key] = {"tool": inst["tool"], "n": inst["n"], "query": query, "view": copy.deepcopy(view)}

    mru = state["toolMru"]
    if (
        not isinstance(mru, list)
        or not all(isinstance(k, str) and k in tools for k in mru)
        or len(set(mru)) != len(mru)
    ):
        raise _invalid("state.toolMru must list open tool tabs, once each")

    labels = state["labels"]
    if not isinstance(labels, dict):
        raise _invalid("state.labels must be an object")
    out_labels = {}
    for key, label in labels.items():
        if key not in open_keys:
            raise _invalid(f"state.labels[{key!r}] must name an open tab")
        out_labels[key] = clean_label(label, f"state.labels[{key!r}]")

    return {
        "tabs": out_tabs,
        "active": active,
        "layout": layout,
        "zoom": out_zoom,
        "instances": out_instances,
        "toolMru": list(mru),
        "labels": out_labels,
    }


def _check_window_id(window_id: Any) -> str:
    if not isinstance(window_id, str) or not _WINDOW_ID.fullmatch(window_id):
        raise _invalid(f"windowId must be 1 to {MAX_WINDOW_ID} printable ASCII characters")
    return window_id


def _check_seq(seq: Any) -> int:
    if not _is_int(seq) or seq < 0:
        raise _invalid("seq must be a non-negative integer")
    return seq


def _clean_result(result: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": result["id"], "status": result["status"]}
    for name, types in _RESULT_EXTRAS.items():
        if name not in result:
            continue
        value = result[name]
        if not isinstance(value, types) or (name != "changed" and isinstance(value, bool)):
            continue
        if isinstance(value, str) and len(value) > MAX_QUERY_BYTES:
            continue
        if isinstance(value, list):
            value = [item for item in value if isinstance(item, str)][:MAX_TABS]
        out[name] = value
    return out


# --- the hub ------------------------------------------------------------------------------


class Stream:
    """One window's event stream: ``hello``, ``batch`` and ``inactive`` events, in order.

    The events route reads it with :meth:`next`; a test drains it with :meth:`drain`. A stream
    ends when it is replaced, when its window loses the slot, on goodbye or when the hub closes;
    the route reports the client going away with :meth:`Hub.disconnect`.
    """

    def __init__(self, hub: Hub, window: str) -> None:
        self.hub = hub
        self.window = window
        self.closed = False
        self._events: deque[dict[str, Any]] = deque()
        self._wake = asyncio.Event()

    def _push(self, event: str, data: dict[str, Any]) -> None:
        if not self.closed:
            self._events.append({"event": event, "data": data})
            self._signal()

    def _close(self) -> None:
        self.closed = True
        self._signal()

    def _signal(self) -> None:
        self._wake.set()

    def drain(self) -> list[dict[str, Any]]:
        """Every event queued so far (after the hub settles its timers)."""
        self.hub.tick()
        events = list(self._events)
        self._events.clear()
        return events

    async def next(self, timeout: float) -> dict[str, Any] | None:
        """The next event; ``None`` after ``timeout`` seconds without one; :class:`StreamEnded` at the end."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            self.hub.tick()
            if self._events:
                return self._events.popleft()
            if self.closed:
                raise StreamEnded
            left = end - loop.time()
            if left <= 0:
                return None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.hub._sleep_for(left))
            except TimeoutError:
                pass


class _Batch:
    __slots__ = ("id", "window", "base", "commands", "enqueued", "delivered", "results", "waiters")

    def __init__(self, batch_id: str, window: str, base: dict | None, commands: list[dict], now: float) -> None:
        self.id = batch_id
        self.window = window
        self.base = base
        self.commands = commands
        self.enqueued = now
        self.delivered: float | None = None
        self.results: list[dict[str, Any]] | None = None
        self.waiters: list[asyncio.Future] = []

    @property
    def resolved(self) -> bool:
        return self.results is not None

    def view(self) -> dict[str, Any]:
        if self.results is None:
            return {"id": self.id, "status": "pending", "results": []}
        return {"id": self.id, "status": "done", "results": copy.deepcopy(self.results)}


class Hub:
    """The workspace slot, the batch queue and the result log; see the module docstring.

    ``clock`` returns milliseconds on a monotonic scale (default ``time.monotonic``), ``wall``
    the ISO-8601 time stamps the document shows, ``epoch`` the id prefix (minted when absent).
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        epoch: str | None = None,
        wall: Callable[[], str] | None = None,
    ) -> None:
        self.clock = clock or _monotonic_ms
        self.wall = wall or _now_iso
        self.epoch = epoch or secrets.token_hex(4)
        self.closed = False
        self._active: str | None = None
        self._seq: int | None = None
        self._shown = False
        self._state: dict[str, Any] | None = None
        self._truncated = False
        self._focused_at: str | None = None
        self._updated_at: str | None = None
        self._absent_since = 0.0
        self._streams: dict[str, Stream] = {}
        self._batches: OrderedDict[str, _Batch] = OrderedDict()
        self._open: list[_Batch] = []  # unresolved, in enqueue order
        self._results: deque[dict[str, Any]] = deque(maxlen=MAX_RESULTS)
        self._next_batch = 1
        self._next_command = 1

    # --- the slot ---------------------------------------------------------------------

    @property
    def active_window(self) -> str | None:
        return self._active

    @property
    def connected(self) -> bool:
        return self._active is not None and self._active in self._streams

    def _present(self, now: float) -> bool:
        """The active window exists: connected, or its stream gone less than 3 s."""
        return self._active is not None and (self.connected or now - self._absent_since < GONE_MS)

    def claim(
        self,
        window_id: str,
        seq: int,
        shown: bool,
        state: Any,
        truncated: bool = False,
        if_vacant: bool = False,
    ) -> dict[str, Any]:
        """Make ``window_id`` the active window; ``{active, previous, epoch}``."""
        window_id, seq = _check_window_id(window_id), _check_seq(seq)
        if not isinstance(shown, bool) or not isinstance(truncated, bool):
            raise _invalid("shown and truncated must be booleans")
        normal = normalise_state(state)
        now = self.tick()
        if if_vacant and self.connected and self._active != window_id:
            raise WorkspaceError("not_vacant", "another window holds the slot")
        previous = self._active
        if previous is not None and previous != window_id:
            self._resolve_window(previous, "window_gone", now)
            stream = self._streams.pop(previous, None)
            if stream is not None:
                stream._push("inactive", {})
                stream._close()
        if previous != window_id or not self.connected:
            self._absent_since = now
        self._active = window_id
        self._write(seq, shown, normal, truncated)
        self._focused_at = self._updated_at
        self._deliver(now)
        return {"active": True, "previous": previous, "epoch": self.epoch}

    def report(self, window_id: str, seq: int, shown: bool, state: Any, truncated: bool = False) -> None:
        """The active window's new state; ``not_active`` from any other window."""
        window_id, seq = _check_window_id(window_id), _check_seq(seq)
        if not isinstance(shown, bool) or not isinstance(truncated, bool):
            raise _invalid("shown and truncated must be booleans")
        normal = normalise_state(state)
        self.tick()
        if window_id != self._active:
            raise WorkspaceError("not_active", "only the window holding the slot reports")
        if self._seq is not None and seq <= self._seq:
            return
        self._write(seq, shown, normal, truncated)

    def goodbye(self, window_id: str) -> None:
        """The window is going away: its stream ends now. Any other window changes nothing."""
        now = self.tick()
        if window_id != self._active:
            return
        stream = self._streams.pop(window_id, None)
        if stream is not None:
            stream._close()
            self._absent_since = now

    def _write(self, seq: int, shown: bool, state: dict[str, Any], truncated: bool) -> None:
        self._seq, self._shown, self._state, self._truncated = seq, shown, state, truncated
        self._updated_at = self.wall()

    # --- streams ----------------------------------------------------------------------

    def connect(self, window_id: str) -> Stream | None:
        """The active window's new stream (``hello`` first), replacing its old one; ``None`` for any other."""
        now = self.tick()
        if self.closed or window_id != self._active:
            return None
        old = self._streams.get(window_id)
        if old is not None:
            old._close()
        stream = self._streams[window_id] = Stream(self, window_id)
        stream._push("hello", {"epoch": self.epoch, "activeWindow": self._active, "seq": self._seq})
        self._deliver(now)
        return stream

    def disconnect(self, stream: Stream) -> None:
        """``stream`` ended (the client went away). Nothing happens unless it is its window's current one."""
        now = self.tick()
        if self._streams.get(stream.window) is stream:
            del self._streams[stream.window]
            stream._close()
            if stream.window == self._active:
                self._absent_since = now

    # --- batches ----------------------------------------------------------------------

    def enqueue(self, commands: Any, base: Any = None, batch_id: Any = None) -> dict[str, Any]:
        """Bind a batch to the active window; the batch view ``{id, status, reason?, results}``."""
        problem = check_batch(commands, base, batch_id)
        if problem:
            return self._refused("invalid_command", problem)
        now = self.tick()
        if batch_id is not None:
            batch = self._batches.get(batch_id)
            if batch is None:
                return self._refused("unknown_batch", "this epoch has no batch by that id")
            self._batches.move_to_end(batch_id)
            return batch.view()
        if self.closed or not self._present(now):
            return self._refused("no_window", "no CANOPY window is connected")
        if base is not None and base["window"] != self._active:
            return self._refused("stale_state", "another window holds the slot now")
        if not self._shown:
            return self._refused("window_hidden", "the active CANOPY window is not shown")
        window = self._active
        pending = sum(len(b.commands) for b in self._open if b.window == window)
        if pending + len(commands) > MAX_PENDING:
            return self._refused("queue_full", f"at most {MAX_PENDING} commands wait per window")
        bound = []
        for command in commands:
            bound.append(
                {
                    "id": f"c-{self.epoch}-{self._next_command}",
                    "cmd": command["cmd"],
                    "args": copy.deepcopy(command.get("args", {})),
                }
            )
            self._next_command += 1
        batch = _Batch(f"b-{self.epoch}-{self._next_batch}", window, copy.deepcopy(base), bound, now)
        self._next_batch += 1
        self._batches[batch.id] = batch
        while len(self._batches) > MAX_BATCH_IDS:
            self._batches.popitem(last=False)
        self._open.append(batch)
        self._deliver(now)
        return batch.view()

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        """The view of a batch this epoch still remembers, or ``None``."""
        self.tick()
        batch = self._batches.get(batch_id)
        return batch.view() if batch is not None else None

    async def wait(self, batch_id: str, wait_s: float) -> dict[str, Any]:
        """The batch's view once it resolves, or ``pending`` after ``wait_s`` (0..10) seconds or at :meth:`close`."""
        wait_s = min(max(float(wait_s), 0.0), MAX_WAIT_S)
        batch = self._batches.get(batch_id)
        if batch is None:
            return self._refused("unknown_batch", "this epoch has no batch by that id")
        loop = asyncio.get_running_loop()
        end = loop.time() + wait_s
        while True:
            self.tick()
            left = end - loop.time()
            if batch.resolved or self.closed or left <= 0:
                return batch.view()
            future = loop.create_future()
            batch.waiters.append(future)
            try:
                await asyncio.wait_for(asyncio.shield(future), self._sleep_for(left))
            except TimeoutError:
                pass
            finally:
                if future in batch.waiters:
                    batch.waiters.remove(future)

    async def submit(
        self, commands: Any, base: Any = None, batch_id: Any = None, wait_s: float = 5.0
    ) -> dict[str, Any]:
        """``set_workspace``: enqueue, wait up to ``wait_s``, and return the document plus ``batch``."""
        batch = self.enqueue(commands, base, batch_id)
        if batch["status"] == "pending" and wait_s:
            batch = await self.wait(batch["id"], wait_s)
        return {**self.document(), "batch": batch}

    def post_result(
        self, window_id: str, batch_id: Any, seq: int, state: Any, results: Any, truncated: bool = False
    ) -> None:
        """The window's one result for a batch it holds; ``not_claimed`` / ``batch_closed`` otherwise."""
        seq = _check_seq(seq)
        if not isinstance(truncated, bool):
            raise _invalid("truncated must be a boolean")
        normal = normalise_state(state)
        now = self.tick()
        batch = self._batches.get(batch_id) if isinstance(batch_id, str) else None
        if batch is None or batch.window != window_id or (batch.delivered is None and not batch.resolved):
            raise WorkspaceError("not_claimed", "that window does not hold that batch")
        if batch.resolved:
            raise WorkspaceError("batch_closed", "that batch is already resolved")
        posted: dict[str, dict[str, Any]] = {}
        if isinstance(results, list):
            for result in results:
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("id"), str)
                    and result.get("status") in POSTED_STATUSES
                ):
                    posted.setdefault(result["id"], _clean_result(result))
        outcome = [posted.get(c["id"], {"id": c["id"], "status": "lost"}) for c in batch.commands]
        if window_id == self._active and (self._seq is None or seq > self._seq):
            self._write(seq, self._shown, normal, truncated)
        self._resolve(batch, outcome)
        self._deliver(now)

    # --- timers and resolution --------------------------------------------------------

    def tick(self) -> float:
        """Settle every deadline due by now; returns now (ms)."""
        now = self.clock()
        while True:
            due = self._next_due()
            if due is None or due[0] > now:
                break
            at, kind, batch = due
            if kind == "gone":
                self._resolve_window(self._active, "window_gone", at)
            elif kind == "lost":
                self._resolve(batch, [{"id": c["id"], "status": "lost"} for c in batch.commands])
            else:
                self._resolve(batch, [{"id": c["id"], "status": "expired"} for c in batch.commands])
            self._deliver(at)
        return now

    def _next_due(self) -> tuple[float, str, _Batch | None] | None:
        best: tuple[float, str, _Batch | None] | None = None
        for batch in self._open:
            if batch.delivered is not None:
                candidate = (batch.delivered + DELIVERY_MS, "lost", batch)
            else:
                candidate = (batch.enqueued + DELIVERY_MS, "expired", batch)
            if best is None or candidate[0] < best[0]:
                best = candidate
        if self._active is not None and not self.connected and any(b.window == self._active for b in self._open):
            candidate = (self._absent_since + GONE_MS, "gone", None)
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best

    def _sleep_for(self, left: float) -> float:
        """Seconds to sleep: ``left``, or less when a deadline falls due sooner."""
        due = self._next_due()
        if due is None:
            return left
        return max(0.0, min(left, (due[0] - self.clock()) / 1000.0 + 0.001))

    def _deliver(self, at: float) -> None:
        window = self._active
        stream = self._streams.get(window) if window is not None else None
        if stream is None:
            return
        mine = [b for b in self._open if b.window == window]
        if any(b.delivered is not None for b in mine):
            return
        for batch in mine:
            if at < batch.enqueued + DELIVERY_MS:
                batch.delivered = at
                data: dict[str, Any] = {"batch": batch.id, "window": window}
                if batch.base is not None:
                    data["base"] = copy.deepcopy(batch.base)
                # Milliseconds the window has left, counted from now: no clock is shared.
                data["deadline"] = max(0, int(at + DELIVERY_MS - self.clock()))
                data["commands"] = copy.deepcopy(batch.commands)
                stream._push("batch", data)
                return

    def _resolve_window(self, window: str | None, status: str, at: float) -> None:
        for batch in [b for b in self._open if b.window == window]:
            self._resolve(batch, [{"id": c["id"], "status": status} for c in batch.commands])

    def _resolve(self, batch: _Batch, results: list[dict[str, Any]]) -> None:
        batch.results = results
        if batch in self._open:
            self._open.remove(batch)
        at = self.wall()
        cmds = {c["id"]: c["cmd"] for c in batch.commands}
        for result in results:
            record = {"id": result["id"], "batch": batch.id, "window": batch.window, "cmd": cmds[result["id"]]}
            record.update({k: v for k, v in result.items() if k != "id"})
            record["at"] = at
            self._results.append(copy.deepcopy(record))
        for future in batch.waiters:
            if not future.done():
                future.set_result(None)

    def _refused(self, reason: str, message: str) -> dict[str, Any]:
        return {"id": None, "status": "refused", "reason": reason, "message": message, "results": []}

    # --- the document and shutdown ----------------------------------------------------

    def document(self) -> dict[str, Any]:
        """``get_workspace``'s document (without ``collections`` and ``sidebar``, which the tool adds)."""
        self.tick()
        return {
            "protocol": PROTOCOL,
            "epoch": self.epoch,
            "activeWindow": self._active,
            "connected": self.connected,
            "shown": self._shown,
            "focusedAt": self._focused_at,
            "updatedAt": self._updated_at,
            "seq": self._seq,
            "truncated": self._truncated,
            "state": copy.deepcopy(self._state),
            "pending": [c["id"] for b in self._open for c in b.commands],
            "results": copy.deepcopy(list(self._results)),
        }

    def close(self) -> None:
        """Shut down: every waiter returns ``pending`` now, every stream ends, nothing new binds."""
        self.closed = True
        for stream in list(self._streams.values()):
            stream._close()
        self._streams.clear()
        for batch in self._batches.values():
            for future in batch.waiters:
                if not future.done():
                    future.set_result(None)
