"""The workspace hub replayed against the shared fixture set (``fixtures/workspace/``).

Every scenario, every batch refusal vector and the hub's side of every route vector run
step by step against a real :class:`~canopy_mcp.workspace.Hub` with epoch ``e1`` and an
injectable clock starting at 0 ms; ``expect`` matches partially as the fixtures' ``about``
lines define. Route cases the hub never sees (a cross-site request, a body over the cap,
the config route) belong to the routes' own tests and are left out here.
"""

from __future__ import annotations

import copy
import json
import time
import asyncio
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from canopy_mcp.workspace import (
    MAX_RESULTS,
    MAX_BATCH_IDS,
    Hub,
    Stream,
    StreamEnded,
    WorkspaceError,
    check_batch,
    normalise_state,
)
from canopy_mcp.tools._common import get_hub, reset_hub

WORKSPACE = Path(__file__).parent / "fixtures" / "workspace"
MAX_BODY_BYTES = 131072


def _load(rel: str) -> Any:
    return json.loads((WORKSPACE / rel).read_text(encoding="ascii"))


def _dir(name: str) -> dict[str, Any]:
    return {p.stem: json.loads(p.read_text(encoding="ascii")) for p in sorted((WORKSPACE / name).glob("*.json"))}


SAMPLE = _load("state.sample.json")
EMPTY = {"tabs": [], "active": None, "layout": None, "zoom": {}, "instances": {}, "toolMru": [], "labels": {}}
SCENARIOS = _dir("scenarios")
REFUSALS = _dir("refusals")
ROUTES = _dir("routes")


class Clock:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> float:
        return self.ms


def _state(token: Any) -> Any:
    if token == "$sample":
        return copy.deepcopy(SAMPLE["state"])
    if token == "$empty":
        return copy.deepcopy(EMPTY)
    return copy.deepcopy(token)


def matches(expect: Any, actual: Any) -> bool:
    """The fixtures' partial match: named keys recursively, lists element by element, else equality."""
    if isinstance(expect, dict):
        return isinstance(actual, dict) and all(k in actual and matches(v, actual[k]) for k, v in expect.items())
    if isinstance(expect, list):
        return isinstance(actual, list) and len(expect) == len(actual) and all(map(matches, expect, actual))
    if isinstance(expect, bool) or isinstance(actual, bool):
        return type(expect) is type(actual) and expect == actual
    return expect == actual


class Replay:
    """Runs fixture steps against one hub."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.hub = Hub(clock=self.clock, epoch="e1", wall=lambda: "2026-10-18T00:00:00.000Z")
        self.streams: dict[str, Stream] = {}

    def _window(self, actor: str) -> str:
        return "w-" + actor.split(":", 1)[1]

    @staticmethod
    def _http(call) -> dict[str, Any]:
        try:
            body = call()
        except WorkspaceError as exc:
            return {"status": exc.status, "body": exc.body()}
        return {"status": 204} if body is None else {"status": 200, "body": body}

    def connect(self, window: str) -> dict[str, Any]:
        stream = self.hub.connect(window)
        if stream is None:
            return {"status": 204}
        self.streams[window] = stream
        return {"status": 200, "events": stream.drain()}

    async def step(self, step: dict[str, Any]) -> Any:
        do = step["do"]
        if step["actor"] == "clock":
            self.clock.ms += step["ms"]
            return None
        if step["actor"] == "agent":
            if do == "get_workspace":
                return self.hub.document()
            kwargs = {k: step[k] for k in ("base", "batch_id") if k in step}
            return await self.hub.submit(step["commands"], wait_s=step["wait_s"], **kwargs)
        window = self._window(step["actor"])
        if do == "claim":
            return self._http(
                lambda: self.hub.claim(
                    window, step["seq"], step["shown"], _state(step["state"]), if_vacant=step.get("ifVacant", False)
                )
            )
        if do == "report":
            return self._http(lambda: self.hub.report(window, step["seq"], step["shown"], _state(step["state"])))
        if do == "goodbye":
            return self._http(lambda: self.hub.goodbye(window))
        if do == "connect":
            return self.connect(window)
        if do == "receive":
            stream = self.streams.get(window)
            return {"events": stream.drain() if stream is not None else []}
        if do == "disconnect":
            self.hub.disconnect(self.streams[window])
            return None
        if do == "result":
            return self._http(
                lambda: self.hub.post_result(window, step["batch"], step["seq"], _state(step["state"]), step["results"])
            )
        raise AssertionError(f"unknown step {do}")

    async def run(self, steps: list[dict[str, Any]], where: str) -> None:
        for index, step in enumerate(steps):
            actual = await self.step(step)
            if "expect" in step:
                assert matches(step["expect"], actual), f"{where} step {index} ({step['actor']} {step['do']}): {actual}"

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        """A route request, as the hub sees it once the route has parsed it."""
        url = urlsplit(request["path"])
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = request.get("body")
        if isinstance(body, dict) and "$file" in body:
            body = _load(body["$file"])
        if isinstance(body, dict) and "state" in body:
            body = {**body, "state": _state(body["state"])}
        method, path = request["method"], url.path
        if method == "POST" and path == "claim":
            return self._http(
                lambda: self.hub.claim(
                    body["windowId"],
                    body["seq"],
                    body["shown"],
                    body["state"],
                    truncated=body.get("truncated", False),
                    if_vacant=query.get("if_vacant") == "1",
                )
            )
        if method == "PUT" and path == "active":
            return self._http(
                lambda: self.hub.report(
                    body["windowId"], body["seq"], body["shown"], body["state"], truncated=body.get("truncated", False)
                )
            )
        if method == "DELETE" and path == "active":
            return self._http(lambda: self.hub.goodbye(query["window"]))
        if method == "GET" and path == "events":
            return self.connect(query["window"])
        if method == "POST" and path.startswith("active/") and path.endswith("/result"):
            window = path.split("/")[1]
            return self._http(
                lambda: self.hub.post_result(window, body["batch"], body["seq"], body["state"], body["results"])
            )
        raise AssertionError(f"no hub call for {method} {path}")


def _route_cases():
    for name, doc in ROUTES.items():
        if name == "config":
            continue
        for case in doc["cases"]:
            request = case["request"]
            site = request.get("headers", {}).get("Sec-Fetch-Site")
            if site not in (None, "same-origin", "none") or case["expect"]["status"] == 413:
                continue  # answered by the route before it reaches the hub
            yield pytest.param(case, id=f"{name}:{case['name']}")


def _refusal_cases():
    for name, doc in REFUSALS.items():
        for case in doc["cases"]:
            yield pytest.param(case, id=f"{name}:{case['name']}")


# --- the shared vectors -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_workspace_hub_replays_scenario(name: str):
    await Replay().run(SCENARIOS[name]["steps"], f"scenarios/{name}")


@pytest.mark.parametrize("case", list(_refusal_cases()))
async def test_workspace_hub_replays_refusal(case: dict):
    await Replay().run(case["steps"], case["name"])


@pytest.mark.parametrize("case", list(_route_cases()))
async def test_workspace_hub_replays_route_vector(case: dict):
    replay = Replay()
    await replay.run(case.get("setup", []), f"{case['name']} setup")
    actual = replay.request(case["request"])
    assert matches(case["expect"], actual), f"{case['name']}: {actual}"
    await replay.run(case.get("then", []), f"{case['name']} then")


def test_workspace_hub_route_vectors_skip_only_what_the_route_answers():
    replayed = {p.id for p in _route_cases()}
    for name, doc in ROUTES.items():
        for case in doc["cases"]:
            if f"{name}:{case['name']}" not in replayed:
                site = case["request"].get("headers", {}).get("Sec-Fetch-Site")
                assert name == "config" or case["expect"]["status"] in (403, 413), (name, case["name"], site)


def test_workspace_hub_matcher_is_partial_but_strict_on_lists():
    assert matches({"a": 1}, {"a": 1, "b": 2})
    assert not matches([1], [1, 2])
    assert not matches({"a": None}, {})
    assert not matches({"a": True}, {"a": 1})


# --- shape and normalisation ---------------------------------------------------------------


def test_workspace_hub_reference_shape_check_agrees_on_every_invalid_command_case():
    for case in REFUSALS["invalid_command"]["cases"]:
        step = case["steps"][-1]
        accepted = check_batch(step["commands"], step.get("base"), step.get("batch_id")) is None
        assert accepted == (step["expect"]["batch"]["status"] == "pending"), case["name"]


def test_workspace_hub_keeps_the_sample_state_unchanged():
    assert normalise_state(SAMPLE["state"]) == SAMPLE["state"]
    reduced = _load("state.reduced.json")
    assert normalise_state(reduced["state"]) == reduced["state"]


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda s: s["tabs"][0].update(key="library/gtb/GTLView?SR=%31"), "non-canonical macro encoding"),
        (lambda s: s["tabs"][0].update(key="library/gtb/GTLView?b=1&a=2"), "macros out of order"),
        (lambda s: s["tabs"][0].update(key="elsewhere/gtb/GTLView"), "unknown category"),
        (lambda s: s["tabs"][2].update(key="@scope"), "unknown tool"),
        (lambda s: s["tabs"][2].update(kind="screen"), "kind disagrees with key"),
        (lambda s: s.update(active="library/nope"), "active not open"),
        (lambda s: s["zoom"].update({"library/nope": {"mode": "fit"}}), "zoom of a closed screen"),
        (lambda s: s["zoom"].update({"library/gtb/GTLView": {"mode": "manual", "percent": 401}}), "zoom percent"),
        (lambda s: s["instances"]["@trace"].update(query="pv=\u00e9"), "query outside ASCII"),
        (lambda s: s["instances"]["@trace"].update(n=2), "instance number disagrees with key"),
        (lambda s: s["instances"].pop("@trace"), "tool tab without instance"),
        (lambda s: s.update(toolMru=["@waveform"]), "mru names a closed tool"),
        (lambda s: s["labels"].update({"library/nope": "x"}), "label for a closed tab"),
        (lambda s: s["layout"]["items"][0].update(key="library/sr/overview"), "undocked tab in the layout"),
        (lambda s: s["layout"]["items"][0].update(size=0), "zero size"),
        (lambda s: s["tabs"][0].update(extra=True), "unknown tab key"),
        (lambda s: s["tabs"].extend([copy.deepcopy(s["tabs"][1])]), "duplicate key"),
    ],
)
def test_workspace_hub_refuses_a_state_that_breaks_a_rule(change, why):
    state = copy.deepcopy(SAMPLE["state"])
    change(state)
    with pytest.raises(WorkspaceError) as caught:
        normalise_state(state)
    assert caught.value.code == "invalid_state" and caught.value.status == 422, why


def test_workspace_hub_accepts_a_numbered_tool_and_a_drafts_screen():
    state = copy.deepcopy(EMPTY)
    state["tabs"] = [
        {"key": "@trace/2", "kind": "tool", "docked": False, "canBack": False, "canForward": False},
        {
            "key": "drafts/beam-tune?A=1&B=x%26y",
            "kind": "screen",
            "docked": False,
            "canBack": False,
            "canForward": False,
        },
    ]
    state["instances"] = {"@trace/2": {"tool": "trace", "n": 2, "query": "", "view": None}}
    assert normalise_state(state)["tabs"][1]["key"] == "drafts/beam-tune?A=1&B=x%26y"
    state["tabs"][0]["key"] = "@trace/1"
    state["instances"] = {"@trace/1": {"tool": "trace", "n": 1, "query": "", "view": None}}
    with pytest.raises(WorkspaceError):
        normalise_state(state)


# --- the hub beyond the vectors ------------------------------------------------------------


def _ready(clock: Clock | None = None) -> tuple[Hub, Stream]:
    hub = Hub(clock=clock or Clock(), epoch="e1")
    hub.claim("w-A", 1, True, copy.deepcopy(SAMPLE["state"]))
    stream = hub.connect("w-A")
    assert stream is not None
    return hub, stream


FOCUS = [{"cmd": "focus", "args": {"key": "@trace"}}]


async def test_workspace_hub_wait_returns_the_result_once_posted():
    hub, stream = _ready()
    batch = hub.enqueue(FOCUS)
    waiting = asyncio.create_task(hub.wait(batch["id"], 5))
    await asyncio.sleep(0)
    assert not waiting.done()
    hub.post_result("w-A", batch["id"], 2, SAMPLE["state"], [{"id": "c-e1-1", "status": "applied", "changed": False}])
    view = await asyncio.wait_for(waiting, 1)
    assert view == {
        "id": "b-e1-1",
        "status": "done",
        "results": [{"id": "c-e1-1", "status": "applied", "changed": False}],
    }
    assert hub.document()["seq"] == 2


async def test_workspace_hub_close_returns_pending_to_waiters_within_a_second():
    hub, stream = _ready()
    batch = hub.enqueue(FOCUS)
    waiting = asyncio.create_task(hub.submit(FOCUS, batch_id=batch["id"], wait_s=10))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    hub.close()
    result = await asyncio.wait_for(waiting, 1)
    assert time.monotonic() - started < 1
    assert result["batch"]["status"] == "pending"
    with pytest.raises(StreamEnded):
        while True:
            await stream.next(1)
    assert hub.enqueue(FOCUS)["reason"] == "no_window"
    assert hub.connect("w-A") is None


async def test_workspace_hub_stream_next_yields_events_then_ends_on_inactive():
    hub, stream = _ready()
    assert (await stream.next(1))["event"] == "hello"
    assert await stream.next(0.01) is None
    hub.enqueue(FOCUS)
    assert (await stream.next(1))["event"] == "batch"
    hub.claim("w-B", 1, True, copy.deepcopy(EMPTY))
    assert (await stream.next(1))["event"] == "inactive"
    with pytest.raises(StreamEnded):
        await stream.next(1)


async def test_workspace_hub_lost_deadline_delivers_the_next_batch_on_the_stream():
    clock = Clock()
    hub, stream = _ready(clock)
    stream.drain()
    hub.enqueue(FOCUS)
    clock.ms = 5000
    hub.enqueue([{"cmd": "focus", "args": {"key": "library/bts/BTSView"}}])
    assert [e["data"]["batch"] for e in stream.drain()] == ["b-e1-1"]
    clock.ms = 10000
    events = stream.drain()
    assert [e["data"]["batch"] for e in events] == ["b-e1-2"]
    assert events[0]["data"]["deadline"] == 10000
    assert hub.batch("b-e1-1")["results"] == [{"id": "c-e1-1", "status": "lost"}]


def test_workspace_hub_disconnect_of_a_replaced_stream_changes_nothing():
    hub, first = _ready()
    second = hub.connect("w-A")
    assert first.closed and not second.closed
    hub.disconnect(first)
    assert hub.document()["connected"] is True
    hub.disconnect(second)
    assert hub.document()["connected"] is False


def test_workspace_hub_batch_bound_in_the_claim_gap_is_window_gone_after_three_seconds():
    clock = Clock()
    hub = Hub(clock=clock, epoch="e1")
    hub.claim("w-A", 1, True, copy.deepcopy(EMPTY))
    batch = hub.enqueue(FOCUS)
    assert batch["status"] == "pending"
    clock.ms = 2999
    assert hub.batch(batch["id"])["status"] == "pending"
    clock.ms = 3000
    assert hub.batch(batch["id"])["results"] == [{"id": "c-e1-1", "status": "window_gone"}]
    assert hub.enqueue(FOCUS)["reason"] == "no_window"


def test_workspace_hub_keeps_the_last_100_results_and_256_batch_ids():
    hub, stream = _ready()
    for n in range(1, MAX_BATCH_IDS + 2):
        batch = hub.enqueue(FOCUS)
        assert batch["id"] == f"b-e1-{n}"
        hub.post_result(
            "w-A", batch["id"], 1, SAMPLE["state"], [{"id": f"c-e1-{n}", "status": "applied", "changed": True}]
        )
    results = hub.document()["results"]
    assert len(results) == MAX_RESULTS and results[-1]["id"] == f"c-e1-{MAX_BATCH_IDS + 1}"
    assert hub.enqueue(FOCUS, batch_id="b-e1-1")["reason"] == "unknown_batch"
    assert hub.enqueue(FOCUS, batch_id="b-e1-2")["status"] == "done"


def test_workspace_hub_missing_and_malformed_results_resolve_lost():
    hub, stream = _ready()
    batch = hub.enqueue(FOCUS + FOCUS)
    hub.post_result("w-A", batch["id"], 2, SAMPLE["state"], [{"id": "c-e1-1", "status": "bogus"}, "x"])
    assert [r["status"] for r in hub.batch(batch["id"])["results"]] == ["lost", "lost"]


def test_workspace_hub_result_below_the_slot_seq_is_stored_but_keeps_the_slot_state():
    hub, stream = _ready()
    batch = hub.enqueue(FOCUS)
    hub.claim("w-A", 5, True, copy.deepcopy(EMPTY))  # the same window re-claims: its batch stands
    hub.post_result("w-A", batch["id"], 4, SAMPLE["state"], [{"id": "c-e1-1", "status": "applied", "changed": True}])
    document = hub.document()
    assert document["seq"] == 5 and document["state"] == EMPTY
    assert document["results"][-1]["status"] == "applied"


def test_workspace_hub_refused_batch_mints_no_ids():
    hub, stream = _ready()
    assert hub.enqueue([])["id"] is None
    assert hub.enqueue(FOCUS)["id"] == "b-e1-1"


def test_workspace_hub_get_hub_is_one_per_process_until_reset():
    reset_hub()
    first = get_hub()
    assert get_hub() is first
    reset_hub()
    assert first.closed
    second = get_hub()
    assert second is not first and second.epoch != first.epoch
    reset_hub()
