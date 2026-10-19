"""The shared workspace fixture set: ``fixtures/workspace/``.

The set is the executable spec of the workspace protocol (protocol 1). It is authored
here; the viewer keeps a byte-identical copy of the whole directory and replays the same
vectors, so the files' bytes are part of the contract: ASCII-only, no trailing
whitespace, exactly one final newline.

This module pins what the set must hold and that it agrees with itself: one command file
per verb with its extras, one rejection file per command rejection reason, one refusal
file per batch refusal reason with a case per syntactic bound, route vectors with the
cross-site refusal on every route, the sample and oversize states, the shown-collections
vector, the per-tool query vectors and the ordered scenarios. Replaying the vectors
against the hub, the routes and the viewer is the job of those modules' own tests.
"""

import re
import copy
import json
from pathlib import Path
from urllib.parse import unquote, parse_qsl

import pytest

WORKSPACE = Path(__file__).parent / "fixtures" / "workspace"

# CONTRACT section 3: verb -> applied extras.
VERBS = {
    "open": {"key", "mode"},
    "focus": set(),
    "close": {"group"},
    "close_others": {"group"},
    "close_right": {"group"},
    "close_all": {"group"},
    "reopen": {"keys"},
    "back": set(),
    "forward": set(),
    "place": set(),
    "remove_tile": set(),
    "reset_layout": {"group"},
    "zoom": {"percent"},
    "open_collection": {"keys", "group"},
    "open_tool": {"key"},
    "set_query": {"previous_query"},
}
DESTRUCTIVE = {"close", "close_others", "close_right", "close_all", "reset_layout"}
# CONTRACT section 4.
REJECTION_REASONS = {
    "unknown_target",
    "invalid_args",
    "instance_cap",
    "tab_limit",
    "nothing_to_reopen",
    "no_history",
    "not_zoomable",
    "operator_busy",
    "stale_state",
}
REFUSAL_REASONS = {"no_window", "window_hidden", "queue_full", "invalid_command", "stale_state", "unknown_batch"}
COMMAND_STATUSES = {"applied", "rejected", "skipped", "expired", "lost", "window_gone"}
BATCH_STATUSES = {"done", "pending", "refused"}
# The hub owns every syntactic bound; the viewer the semantic ones.
HUB_BOUNDS = {
    "command-count",
    "command-shape",
    "verb-enum",
    "edge-enum",
    "mode-enum",
    "step-enum",
    "tool-enum",
    "arg-type",
    "arg-required",
    "arg-unknown",
    "arg-exclusive",
    "macro-count",
    "macro-length",
    "macro-charset",
    "query-bytes",
    "zoom-percent",
    "base-shape",
    "batch-id-type",
}
# Bounds with an accepted side the vectors pin as well.
HUB_BOUNDS_WITH_LIMIT = {"command-count", "macro-count", "macro-length", "macro-charset", "query-bytes", "zoom-percent"}
VIEWER_BOUNDS = {
    "trace-range",
    "trace-grammar",
    "trace-pvs",
    "correlation-pvs",
    "correlation-grammar",
    "waveform-pvs",
    "waveform-grammar",
    "place-beside-self",
}
TAB_OPENING_VERBS = {"open", "open_tool", "open_collection", "reopen"}
SCENARIOS = {
    "active-switch",
    "reconnect-reclaim",
    "double-result",
    "expiry",
    "lost-at-deadline",
    "window-gone",
    "hidden-window",
    "stale-state-hub",
    "stale-state-viewer",
}
ROUTES = {"config", "claim", "report", "goodbye", "events", "result"}
ACTORS = {"window:A", "window:B", "agent", "clock"}
ACTIONS = {
    "window": {"claim", "report", "goodbye", "connect", "receive", "disconnect", "result"},
    "agent": {"get_workspace", "set_workspace"},
    "clock": {"advance"},
}
STATE_TOKENS = {"$sample", "$empty"}
STATE_KEYS = {"tabs", "active", "layout", "zoom", "instances", "toolMru", "labels"}
BODY_KEYS = {"windowId", "seq", "shown", "state", "truncated"}
TAB_KEYS = {"key", "kind", "docked", "canBack", "canForward"}
TOOLS = {"trace": "pv", "correlation": "y", "waveform": "pv"}
MAX_PVS = {"trace": 12, "correlation": 8, "waveform": 8}
MAX_BODY_BYTES = 131072
MAX_QUERY_BYTES = 4096
MAX_COMMANDS = 20
MAX_TABS = 50
MAX_LAYOUT_DEPTH = 6


def _load(rel: str):
    return json.loads((WORKSPACE / rel).read_text(encoding="ascii"))


def _dir(name: str) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="ascii")) for p in sorted((WORKSPACE / name).glob("*.json"))}


ALL_FILES = sorted(p.relative_to(WORKSPACE).as_posix() for p in WORKSPACE.rglob("*") if p.is_file())
COMMANDS = _dir("commands")
REJECTIONS = _dir("rejections")
REFUSALS = _dir("refusals")
SCENARIO_DOCS = _dir("scenarios")
ROUTE_DOCS = _dir("routes")
QUERIES = _dir("queries")
CONTEXT = _load("context.json")["context"]
SAMPLE = _load("state.sample.json")
OVERSIZE = _load("state.oversize.json")
REDUCED = _load("state.reduced.json")


def _compact_bytes(doc) -> int:
    return len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _context(case: dict) -> dict:
    merged = copy.deepcopy(CONTEXT)
    merged.update(copy.deepcopy(case.get("context", {})))
    return merged


# --- a reference shape check: the hub's syntactic bounds as the vectors state them --------

_ENUMS = {
    "mode": {"polite", "append", "replace"},
    "edge": {"left", "right", "above", "below"},
    "step": {"in", "out", "fit", "actual"},
    "tool": set(TOOLS),
}
_ARGS = {
    "open": ({"ref"}, {"macros", "mode", "key"}),
    "focus": ({"key"}, set()),
    "close": ({"key"}, set()),
    "close_others": ({"key"}, set()),
    "close_right": ({"key"}, set()),
    "close_all": (set(), set()),
    "reopen": (set(), {"id"}),
    "back": ({"key"}, set()),
    "forward": ({"key"}, set()),
    "place": ({"key"}, {"edge", "beside"}),
    "remove_tile": ({"key"}, set()),
    "reset_layout": (set(), set()),
    "zoom": ({"key"}, {"step", "percent"}),
    "open_collection": ({"ref"}, {"layout"}),
    "open_tool": ({"tool"}, {"query"}),
    "set_query": ({"key", "query"}, set()),
}
_MACRO_VALUE = re.compile(r"^[\x21-\x7e]{0,64}$")


def _arg_ok(name: str, value) -> bool:
    if name in _ENUMS:
        return isinstance(value, str) and value in _ENUMS[name]
    if name == "macros":
        return (
            isinstance(value, dict)
            and len(value) <= 16
            and all(isinstance(k, str) and k != "" for k in value)
            and all(isinstance(v, str) and _MACRO_VALUE.match(v) for v in value.values())
        )
    if name == "layout":
        return isinstance(value, bool)
    if name == "percent":
        return isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 400
    if name == "query":
        return isinstance(value, str) and len(value.encode("utf-8")) <= MAX_QUERY_BYTES
    return isinstance(value, str)


def _command_ok(command) -> bool:
    if not isinstance(command, dict) or "cmd" not in command or not set(command) <= {"cmd", "args"}:
        return False
    if command["cmd"] not in _ARGS:
        return False
    args = command.get("args", {})
    if not isinstance(args, dict):
        return False
    required, optional = _ARGS[command["cmd"]]
    if not required <= set(args) or not set(args) <= required | optional:
        return False
    if not all(_arg_ok(name, value) for name, value in args.items()):
        return False
    if command["cmd"] == "zoom" and len({"step", "percent"} & set(args)) != 1:
        return False
    if command["cmd"] == "open" and ("key" in args) != (args.get("mode") == "replace"):
        return False
    return True


def _hub_accepts(step: dict) -> bool:
    commands = step["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_COMMANDS:
        return False
    if "base" in step:
        base = step["base"]
        if not (isinstance(base, dict) and set(base) == {"window", "seq"} and isinstance(base["window"], str)):
            return False
        if not isinstance(base["seq"], int) or isinstance(base["seq"], bool):
            return False
    if "batch_id" in step and not isinstance(step["batch_id"], str):
        return False
    return all(_command_ok(c) for c in commands)


def _all_steps():
    for name, doc in SCENARIO_DOCS.items():
        for step in doc["steps"]:
            yield f"scenarios/{name}", step
    for name, doc in REFUSALS.items():
        for case in doc["cases"]:
            for step in case["steps"]:
                yield f"refusals/{name}:{case['name']}", step
    for name, doc in ROUTE_DOCS.items():
        for case in doc["cases"]:
            for step in case.get("setup", []) + case.get("then", []):
                yield f"routes/{name}:{case['name']}", step


# --- the files themselves -------------------------------------------------------------------


def test_workspace_fixtures_hold_exactly_the_planned_files():
    expected = (
        {f"commands/{verb}.json" for verb in VERBS}
        | {f"rejections/{reason}.json" for reason in REJECTION_REASONS}
        | {f"refusals/{reason}.json" for reason in REFUSAL_REASONS}
        | {f"routes/{route}.json" for route in ROUTES}
        | {f"scenarios/{name}.json" for name in SCENARIOS}
        | {f"queries/{tool}.json" for tool in TOOLS}
        | {"context.json", "state.sample.json", "state.oversize.json", "state.reduced.json", "collections-shown.json"}
    )
    assert set(ALL_FILES) == expected


@pytest.mark.parametrize("rel", ALL_FILES)
def test_workspace_fixtures_file_hygiene(rel: str):
    data = (WORKSPACE / rel).read_bytes()
    assert data.isascii()
    assert b"\r" not in data and b"\t" not in data
    assert [n for n, line in enumerate(data.split(b"\n"), 1) if line != line.rstrip()] == []
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    json.loads(data)


@pytest.mark.parametrize("rel", [r for r in ALL_FILES if not r.startswith("state.")])
def test_workspace_fixtures_vector_files_carry_about_and_unique_case_names(rel: str):
    doc = _load(rel)
    assert isinstance(doc["about"], list) and all(isinstance(line, str) for line in doc["about"])
    if "cases" in doc:
        names = [case["name"] for case in doc["cases"]]
        assert names and len(names) == len(set(names)), rel


# --- states -------------------------------------------------------------------------------


def _check_layout(node: dict, keys: set[str], placed: set[str], depth: int, root: bool) -> None:
    assert depth <= MAX_LAYOUT_DEPTH
    allowed = {"split", "items", "active"} if root else ({"items", "size"} if "items" in node else {"key", "size"})
    assert set(node) <= allowed, node
    if "size" in node:
        assert isinstance(node["size"], (int, float)) and not isinstance(node["size"], bool) and node["size"] > 0
    if "key" in node:
        assert node["key"] in keys and node["key"] not in placed
        placed.add(node["key"])
        return
    if root:
        assert node["split"] in ("row", "column")
    assert len(node["items"]) >= 2
    for item in node["items"]:
        _check_layout(item, keys, placed, depth + 1, False)


def _check_state(state: dict) -> None:
    assert set(state) == STATE_KEYS
    assert len(state["tabs"]) <= MAX_TABS
    keys = [t["key"] for t in state["tabs"]]
    assert len(keys) == len(set(keys))
    for t in state["tabs"]:
        assert set(t) == TAB_KEYS and t["kind"] in ("screen", "tool")
        assert t["kind"] == ("tool" if t["key"].startswith("@") else "screen")
    assert state["active"] is None or state["active"] in keys
    docked = {t["key"] for t in state["tabs"] if t["docked"]}
    if state["layout"] is not None:
        placed: set[str] = set()
        _check_layout(state["layout"], docked, placed, 1, True)
        assert placed == docked
        if "active" in state["layout"]:
            assert state["layout"]["active"] in docked
    tools = {k for k in keys if k.startswith("@")}
    assert set(state["instances"]) == tools
    for key, inst in state["instances"].items():
        assert set(inst) == {"tool", "n", "query", "view"}
        assert key == (f"@{inst['tool']}" if inst["n"] == 1 else f"@{inst['tool']}/{inst['n']}")
        assert len(inst["query"].encode()) <= MAX_QUERY_BYTES and inst["query"].isascii()
    screens = {k.split("?", 1)[0] for k in keys if not k.startswith("@")}
    assert set(state["zoom"]) <= screens
    for zoom in state["zoom"].values():
        assert zoom == {"mode": "fit"} or (zoom["mode"] == "manual" and 10 <= zoom["percent"] <= 400)
    assert set(state["toolMru"]) <= tools
    assert set(state["labels"]) <= set(keys)
    assert all(len(label) <= 120 for label in state["labels"].values())


@pytest.mark.parametrize("name", ["state.sample.json", "state.oversize.json", "state.reduced.json"])
def test_workspace_fixtures_state_bodies_follow_the_schema(name: str):
    body = _load(name)
    assert set(body) <= BODY_KEYS and {"windowId", "seq", "shown", "state"} <= set(body)
    _check_state(body["state"])


def test_workspace_fixtures_context_state_is_the_sample():
    assert CONTEXT["state"] == SAMPLE["state"]
    assert CONTEXT["seq"] == SAMPLE["seq"]


def _drop_plot(query: str) -> str:
    return "&".join(p for p in query.split("&") if p.split("=", 1)[0] != "plot")


def test_workspace_fixtures_oversize_reduces_to_the_reduced_form():
    assert _compact_bytes(OVERSIZE) > MAX_BODY_BYTES and "truncated" not in OVERSIZE
    expected = copy.deepcopy(OVERSIZE)
    for inst in expected["state"]["instances"].values():
        inst["query"] = _drop_plot(inst["query"])
    assert _compact_bytes(expected) > MAX_BODY_BYTES, "the vector exercises dropping labels too"
    expected["state"]["labels"] = {}
    expected["truncated"] = True
    assert REDUCED == expected
    assert _compact_bytes(REDUCED) <= MAX_BODY_BYTES


# --- collections.shown -------------------------------------------------------------------


def test_workspace_fixtures_collections_shown_vector_is_consistent():
    doc = _load("collections-shown.json")
    assert len(doc["cases"]) >= 5
    for case in doc["cases"]:
        ticked, unticked = set(case["state"]["ticked"]), set(case["state"]["unticked"])
        shown = [
            c["ref"] for c in case["collections"] if (c["default"] or c["ref"] in ticked) and c["ref"] not in unticked
        ]
        assert case["expect"] == shown, case["name"]


# --- commands -----------------------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_workspace_fixtures_command_vector_per_verb(verb: str):
    doc = COMMANDS[verb]
    assert doc["verb"] == verb
    assert set(doc["extras"]) == VERBS[verb]
    assert doc["destructive"] == (
        True if verb in DESTRUCTIVE else ("with layout" if verb == "open_collection" else False)
    )
    seen: set[str] = set()
    for case in doc["cases"]:
        assert case["command"]["cmd"] == verb
        assert _command_ok(case["command"]), case["name"]
        result = case["result"]
        assert result["status"] == "applied" and isinstance(result["changed"], bool)
        assert set(result) - {"status", "changed"} <= VERBS[verb], case["name"]
        if verb in DESTRUCTIVE:
            assert ("group" in result) == result["changed"], case["name"]
        seen |= set(result) - {"status", "changed"}
        if "after" in case:
            assert set(case["after"]) <= {"tabs", "active"}
            if "active" in case["after"] and case["after"]["active"] is not None:
                assert case["after"]["active"] in case["after"]["tabs"]
        assert set(_context(case)) == set(CONTEXT)
    assert seen == VERBS[verb], "every extra appears in a case"


# --- rejections ---------------------------------------------------------------------------


def _check_results(batch: dict, results: list[dict], reason: str) -> None:
    ids = [c["id"] for c in batch["commands"]]
    assert [r["id"] for r in results] == ids
    statuses = [r["status"] for r in results]
    first = statuses.index("rejected")
    assert set(statuses[:first]) <= {"applied"}
    assert results[first]["reason"] == reason
    for r in results[first + 1 :]:
        assert r == {"id": r["id"], "status": "skipped", "after": ids[first]}


@pytest.mark.parametrize("reason", sorted(REJECTION_REASONS))
def test_workspace_fixtures_rejection_vector_per_reason(reason: str):
    doc = REJECTIONS[reason]
    assert doc["reason"] == reason and doc["cases"]
    for case in doc["cases"]:
        batch = case["batch"]
        step = {"commands": [{"cmd": c["cmd"], "args": c["args"]} for c in batch["commands"]]}
        if "base" in batch:
            step["base"] = batch["base"]
        assert _hub_accepts(step), f"{case['name']}: a rejection vector must pass the hub's shape check"
        _check_results(batch, case["results"], reason)
        assert set(_context(case)) == set(CONTEXT)


def test_workspace_fixtures_invalid_args_names_every_viewer_bound():
    assert {case.get("bound") for case in REJECTIONS["invalid_args"]["cases"]} == VIEWER_BOUNDS


def test_workspace_fixtures_tab_limit_has_a_case_per_opening_verb():
    cases = REJECTIONS["tab_limit"]["cases"]
    assert {case["bound"] for case in cases} == TAB_OPENING_VERBS
    for case in cases:
        assert case["batch"]["commands"][0]["cmd"] == case["bound"]
        assert len(_context(case)["state"]["tabs"]) >= MAX_TABS - 1


def test_workspace_fixtures_stale_state_rejection_covers_batch_start_and_mid_batch():
    cases = REJECTIONS["stale_state"]["cases"]
    assert any(c["batch"]["base"]["seq"] < CONTEXT["seq"] for c in cases)
    assert any("operatorChangeAfter" in c for c in cases)


# --- refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(REFUSAL_REASONS))
def test_workspace_fixtures_refusal_vector_per_reason(reason: str):
    doc = REFUSALS[reason]
    assert doc["reason"] == reason
    refused = [
        step
        for case in doc["cases"]
        for step in case["steps"]
        if step["actor"] == "agent" and step["do"] == "set_workspace" and step["expect"]["batch"]["status"] == "refused"
    ]
    assert refused and all(s["expect"]["batch"]["reason"] == reason for s in refused)
    for step in refused:
        assert step["expect"]["batch"]["id"] is None and step["expect"]["batch"]["results"] == []


def test_workspace_fixtures_invalid_command_has_a_case_per_hub_bound():
    cases = REFUSALS["invalid_command"]["cases"]
    assert {case["bound"] for case in cases} == HUB_BOUNDS
    accepted = set()
    for case in cases:
        step = case["steps"][-1]
        verdict = step["expect"]["batch"]["status"]
        assert verdict in ("refused", "pending")
        assert _hub_accepts(step) == (verdict == "pending"), case["name"]
        if verdict == "pending":
            accepted.add(case["bound"])
    assert accepted == HUB_BOUNDS_WITH_LIMIT


def test_workspace_fixtures_every_enqueued_batch_agrees_with_the_hub_bounds():
    for where, step in _all_steps():
        if step["actor"] != "agent" or step["do"] != "set_workspace" or where.startswith("refusals/invalid_command"):
            continue
        assert _hub_accepts(step), where


# --- scenarios and steps ------------------------------------------------------------------


def _check_step(where: str, step: dict) -> None:
    assert step["actor"] in ACTORS, where
    kind = step["actor"].split(":", 1)[0]
    assert step["do"] in ACTIONS[kind], where
    if step["do"] in ("claim", "report", "result"):
        state = step["state"]
        if isinstance(state, str):
            assert state in STATE_TOKENS, where
        else:
            assert isinstance(state, dict)
    if step["do"] == "advance":
        assert isinstance(step["ms"], int) and step["ms"] > 0
    if step["do"] == "set_workspace":
        assert 0 <= step["wait_s"] <= 10
        batch = step["expect"]["batch"]
        assert batch["status"] in BATCH_STATUSES
        if batch["status"] == "refused":
            assert batch["reason"] in REFUSAL_REASONS
        if batch["status"] == "pending":
            assert batch["results"] == []
        for r in batch.get("results", []):
            assert r["status"] in COMMAND_STATUSES
    if step["do"] == "result":
        for r in step["results"]:
            assert r["status"] in {"applied", "rejected", "skipped"}
            if r["status"] == "rejected":
                assert r["reason"] in REJECTION_REASONS
    if step["do"] == "get_workspace" and "protocol" in step["expect"]:
        assert step["expect"]["protocol"] == 1


def test_workspace_fixtures_scenarios_are_the_planned_set():
    assert set(SCENARIO_DOCS) == SCENARIOS
    for name, doc in SCENARIO_DOCS.items():
        assert doc["scenario"] == name and doc["steps"]


def test_workspace_fixtures_steps_name_only_known_actors_and_actions():
    for where, step in _all_steps():
        _check_step(where, step)


def test_workspace_fixtures_scenarios_use_both_windows_the_agent_and_the_clock():
    actors = {step["actor"] for doc in SCENARIO_DOCS.values() for step in doc["steps"]}
    assert actors == ACTORS


def test_workspace_fixtures_scenarios_reach_every_command_end_status():
    statuses = {
        r["status"]
        for doc in SCENARIO_DOCS.values()
        for step in doc["steps"]
        for r in (step.get("results", []) + step.get("expect", {}).get("batch", {}).get("results", []))
    }
    assert statuses == COMMAND_STATUSES


def test_workspace_fixtures_stale_state_comes_in_both_forms():
    hub = SCENARIO_DOCS["stale-state-hub"]["steps"]
    assert any(
        s["do"] == "set_workspace"
        and s["expect"]["batch"].get("reason") == "stale_state"
        and s["base"]["window"] != "w-B"
        for s in hub
    )
    viewer = SCENARIO_DOCS["stale-state-viewer"]["steps"]
    posted = [s for s in viewer if s["do"] == "result"]
    assert posted and posted[0]["results"][0] == {"id": "c-e1-1", "status": "rejected", "reason": "stale_state"}
    assert all(r == {"id": r["id"], "status": "skipped", "after": "c-e1-1"} for r in posted[0]["results"][1:])


def test_workspace_fixtures_get_workspace_reports_protocol_1():
    assert any(step["do"] == "get_workspace" and step["expect"].get("protocol") == 1 for _, step in _all_steps())


# --- routes -------------------------------------------------------------------------------


def test_workspace_fixtures_routes_are_the_planned_set_and_every_route_refuses_cross_site():
    assert set(ROUTE_DOCS) == ROUTES
    for name, doc in ROUTE_DOCS.items():
        assert doc["route"] == name
        if name == "config":
            continue
        crosses = [
            c
            for c in doc["cases"]
            if c["request"].get("headers", {}).get("Sec-Fetch-Site") not in (None, "same-origin", "none")
        ]
        assert crosses, name
        assert all(c["expect"] == {"status": 403, "body": {"error": "cross_site"}} for c in crosses), name


def test_workspace_fixtures_routes_cover_the_statuses():
    pairs = {
        (c["expect"]["status"], c["expect"].get("body", {}).get("error"))
        for doc in ROUTE_DOCS.values()
        for c in doc["cases"]
    }
    for pair in [
        (200, None),
        (204, None),
        (403, "cross_site"),
        (404, None),
        (409, "not_active"),
        (409, "not_vacant"),
        (409, "not_claimed"),
        (409, "batch_closed"),
        (413, "too_large"),
        (422, "invalid_state"),
    ]:
        assert pair in pairs, pair


def test_workspace_fixtures_no_route_accepts_commands():
    for doc in ROUTE_DOCS.values():
        for case in doc["cases"]:
            body = case["request"].get("body")
            assert not (isinstance(body, dict) and "commands" in body), case["name"]
            assert "enqueue" not in case["request"]["path"]


def test_workspace_fixtures_config_advertises_protocol_1_only_with_the_opt_in():
    cases = ROUTE_DOCS["config"]["cases"]
    on = [c for c in cases if c["optIn"] and c["request"]["path"] == "/config.json"]
    assert on and on[0]["expect"]["body"]["workspace"] == {"path": "workspace", "protocol": 1}
    off = [c for c in cases if not c["optIn"]]
    assert {c["expect"]["status"] for c in off if c["request"]["path"] != "/config.json"} == {404}


def test_workspace_fixtures_route_file_bodies_resolve():
    for doc in ROUTE_DOCS.values():
        for case in doc["cases"]:
            body = case["request"].get("body")
            if isinstance(body, dict) and "$file" in body:
                assert (WORKSPACE / body["$file"]).is_file()


# --- queries ------------------------------------------------------------------------------


def _decodes(part: str) -> bool:
    try:
        unquote(part, errors="strict")
    except UnicodeDecodeError:
        return False
    return not re.search(r"%(?![0-9A-Fa-f]{2})", part)


def _viewer_verdict(tool: str, query: str) -> str:
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        return "invalid_command"
    pairs = [] if query == "" else query.split("&")
    if not all(_decodes(part) for pair in pairs for part in pair.split("=", 1)):
        return "invalid_args"
    keys = [unquote(pair.split("=", 1)[0]) for pair in pairs]
    if tool == "trace" and not any(k in ("pv", "range") for k in keys):
        return "invalid_args"
    values = {v.strip() for k, v in parse_qsl(query.removeprefix("?"), keep_blank_values=True) if k == TOOLS[tool]}
    if len(values - {""}) > MAX_PVS[tool]:
        return "invalid_args"
    return "range?" if tool == "trace" and "range" in keys else "accepted"


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_workspace_fixtures_query_vectors_per_tool(tool: str):
    doc = QUERIES[tool]
    assert doc["tool"] == tool and "d52a7b9" in doc["source"]
    verdicts = set()
    for case in doc["cases"]:
        assert case["verdict"] in ("accepted", "invalid_args", "invalid_command")
        assert " > " in case["test"]
        expected = _viewer_verdict(tool, case["query"])
        if expected == "range?":
            assert case["verdict"] in ("accepted", "invalid_args"), case["name"]
        else:
            assert case["verdict"] == expected, case["name"]
        verdicts.add(case["verdict"])
    assert {"accepted", "invalid_args", "invalid_command"} <= verdicts


def test_workspace_fixtures_rejected_queries_match_their_tool_rules():
    for case in REJECTIONS["invalid_args"]["cases"]:
        command = case["batch"]["commands"][0]
        if "query" not in command["args"]:
            continue
        tool = command["args"].get("tool") or "trace"
        assert _viewer_verdict(tool, command["args"]["query"]) in ("invalid_args", "range?"), case["name"]
