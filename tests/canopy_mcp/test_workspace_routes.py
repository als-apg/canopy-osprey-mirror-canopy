"""The workspace routes, replayed over HTTP against the shared route vectors.

Every case of ``fixtures/workspace/routes/*.json`` runs here through the real app: the
setup and follow-up steps act on the process hub directly (as the hub's own replay
does), the one request goes through the ASGI app, so the cross-site, body-cap and
opt-in cases the hub never sees are covered too. The event stream is driven at the
ASGI level so it can stay open while the follow-up steps run.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any
from pathlib import Path

import pytest
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

import canopy_mcp.workspace as hubs
from canopy_mcp.app import create_app
from canopy_mcp.config import Settings
from canopy_mcp.workspace import Hub
from canopy_mcp.shared_library import library_config
from canopy_mcp.workspace_routes import (
    MAX_BODY_BYTES,
    WORKSPACE_PATH,
    exit_watch,
    build_workspace_routes,
)

from .test_workspace_hub import ROUTES, Replay, _load, _state, matches

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
SAMPLE_BODY = {"windowId": "w-A", "seq": 41, "shown": True, "state": "$sample"}


@pytest.fixture(autouse=True)
def fresh_hub():
    hubs.reset_hub()
    AppStatus.should_exit = False
    yield
    hubs.reset_hub()
    AppStatus.should_exit = False


def _settings(tmp_path: Path, workspace: bool) -> Settings:
    return Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, pv_mode="mock", workspace=workspace)


def _app(tmp_path: Path, workspace: bool = True):
    return create_app(_settings(tmp_path, workspace), startup_hooks=[])


def _body_bytes(request: dict[str, Any]) -> bytes | None:
    if "body" not in request:
        return None
    body = request["body"]
    if isinstance(body, dict) and "$file" in body:
        body = _load(body["$file"])
    if isinstance(body, dict) and "state" in body:
        body = {**body, "state": _state(body["state"])}
    raw = json.dumps(body, separators=(",", ":")).encode("ascii")
    if "padTo" in request:
        assert len(raw) <= request["padTo"], "the fixture's body is already over its padTo"
        raw += b" " * (request["padTo"] - len(raw))
    return raw


def _url(path: str) -> str:
    return f"/{WORKSPACE_PATH}" + (f"/{path}" if path else "")


def _parse_events(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, list[str]] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            fields.setdefault(name, []).append(value[1:] if value.startswith(" ") else value)
        if "event" in fields or "data" in fields:
            event: dict[str, Any] = {"event": fields.get("event", ["message"])[0]}
            data = "\n".join(fields.get("data", []))
            event["data"] = json.loads(data) if data else None
            events.append(event)
    return events


class Exchange:
    """One request driven at the ASGI level; the client goes away only on :meth:`close`."""

    def __init__(self, app, method: str, url: str, headers: dict[str, str] | None, body: bytes | None) -> None:
        path, _, query = url.partition("?")
        raw_headers = [(b"host", b"testserver")]
        for name, value in (headers or {}).items():
            raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
        if body is not None:
            raw_headers.append((b"content-type", b"application/json"))
            raw_headers.append((b"content-length", str(len(body)).encode()))
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        self.body = body or b""
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.done = False
        self._sent = False
        self._gone = asyncio.Event()
        self._more = asyncio.Event()
        self.task = asyncio.create_task(app(self.scope, self._receive, self._send))
        self.task.add_done_callback(lambda _: self._more.set())

    async def _receive(self) -> dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            self.chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                self.done = True
        self._more.set()

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode()

    @property
    def events(self) -> list[dict[str, Any]]:
        return _parse_events(self.text)

    async def until(self, want_events: int | None = None, timeout: float = 3.0) -> None:
        """Wait for the response to finish, or (a stream) for ``want_events`` events plus a short grace."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not self.done and not (
            want_events is not None and self.status == 200 and len(self.events) >= want_events
        ):
            if self.task.done():
                self.task.result()
                break
            self._more.clear()
            left = end - loop.time()
            assert left > 0, f"no answer in {timeout} s: status {self.status}, body {self.text!r}"
            try:
                await asyncio.wait_for(self._more.wait(), left)
            except TimeoutError:
                pass
        if not self.done and not self.task.done():
            await asyncio.sleep(0.05)  # anything more the stream sends right away

    async def close(self) -> None:
        self._gone.set()
        await asyncio.wait_for(self.task, 3.0)

    def json(self) -> Any:
        return json.loads(self.text)


def _actual(exchange: Exchange) -> dict[str, Any]:
    actual: dict[str, Any] = {"status": exchange.status}
    if exchange.headers.get("content-type", "").startswith("text/event-stream"):
        actual["events"] = exchange.events
    elif exchange.headers.get("content-type", "").startswith("application/json"):
        actual["body"] = exchange.json()
    return actual


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    """The case's ``expect`` without ``absent`` (keys the body must not have, checked apart)."""
    return {k: v for k, v in case["expect"].items() if k != "absent"}


def _cases(*, config: bool):
    for name, doc in ROUTES.items():
        if (name == "config") != config:
            continue
        for case in doc["cases"]:
            yield pytest.param(case, id=f"{name}:{case['name']}")


# --- the shared route vectors -------------------------------------------------------------


@pytest.mark.parametrize("case", list(_cases(config=False)))
async def test_workspace_routes_replay_route_vector(case: dict, tmp_path: Path):
    replay = Replay()
    hubs._hub = replay.hub
    app = _app(tmp_path)
    await replay.run(case.get("setup", []), f"{case['name']} setup")
    request = case["request"]
    exchange = Exchange(app, request["method"], _url(request["path"]), request.get("headers"), _body_bytes(request))
    try:
        await exchange.until(want_events=len(case["expect"].get("events", [])) or None)
        actual = _actual(exchange)
        assert matches(_expected(case), actual), f"{case['name']}: {actual}"
        for key in case["expect"].get("absent", []):
            assert key not in actual["body"], key
        await replay.run(case.get("then", []), f"{case['name']} then")
    finally:
        await exchange.close()


@pytest.mark.parametrize("case", list(_cases(config=True)))
async def test_workspace_routes_replay_config_vector(case: dict, tmp_path: Path):
    app = _app(tmp_path, workspace=case["optIn"])
    request = case["request"]
    url = request["path"] if request["path"].startswith("/") else _url(request["path"])
    exchange = Exchange(app, request["method"], url, request.get("headers"), _body_bytes(request))
    try:
        await exchange.until()
        actual = _actual(exchange)
        assert matches(_expected(case), actual), f"{case['name']}: {actual}"
        for key in case["expect"].get("absent", []):
            assert key not in actual["body"], key
    finally:
        await exchange.close()


def test_workspace_routes_every_vector_is_replayed():
    replayed = {p.id for p in _cases(config=False)} | {p.id for p in _cases(config=True)}
    assert replayed == {f"{n}:{c['name']}" for n, d in ROUTES.items() for c in d["cases"]}
    # The cross-site and body-cap cases the hub's replay leaves to the routes are among them.
    sites = {c["request"].get("headers", {}).get("Sec-Fetch-Site") for d in ROUTES.values() for c in d["cases"]}
    assert {"cross-site", "same-site"} <= sites


# --- the surface beyond the vectors -------------------------------------------------------


def test_workspace_routes_accept_no_commands(tmp_path: Path):
    """No route under /workspace accepts a batch; only the documented four paths exist."""
    paths = {route.path for route in build_workspace_routes()}
    assert paths == {
        "/workspace/claim",
        "/workspace/active",
        "/workspace/events",
        "/workspace/active/{window_id}/result",
    }
    app = _app(tmp_path)
    commands = {"commands": [{"cmd": "close_all", "args": {}}]}
    with TestClient(app) as client:
        for path in ("", "/batch", "/batches", "/commands", "/enqueue", "/active/w-A/batch", "/active/w-A"):
            for method in ("POST", "PUT"):
                r = client.request(method, f"/workspace{path}", json=commands)
                assert r.status_code in (404, 405), (method, path, r.status_code)
        assert hubs.get_hub().document()["pending"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "claim"),
        ("PUT", "active"),
        ("DELETE", "active?window=w-A"),
        ("GET", "events?window=w-A"),
        ("POST", "active/w-A/result"),
    ],
)
@pytest.mark.parametrize("site", ["cross-site", "same-site", " Cross-Site "])
def test_workspace_routes_refuse_cross_site_before_the_hub(tmp_path: Path, method: str, path: str, site: str):
    hub = hubs.get_hub()
    hub.claim("w-A", 41, True, _state("$sample"))
    before = hub.document()
    with TestClient(_app(tmp_path)) as client:
        r = client.request(method, _url(path), content=b"not json", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert r.json()["error"] == "cross_site"
    assert r.headers["cache-control"] == "no-cache"
    assert hubs.get_hub() is not hub or hub.document() == before


@pytest.mark.parametrize("site", ["same-origin", "none", "NONE"])
def test_workspace_routes_allow_same_origin_and_direct_requests(tmp_path: Path, site: str):
    with TestClient(_app(tmp_path)) as client:
        body = {**SAMPLE_BODY, "state": _state("$sample")}
        r = client.post(_url("claim"), json=body, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 200, r.text


def test_workspace_routes_answer_405_with_allow(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        for method, path, allow in (
            ("GET", "claim", "POST"),
            ("POST", "active", "PUT, DELETE"),
            ("POST", "events?window=w-A", "GET"),
            ("GET", "active/w-A/result", "POST"),
        ):
            r = client.request(method, _url(path))
            assert r.status_code == 405, (method, path)
            assert r.headers["allow"] == allow
            assert r.json()["error"] == "method_not_allowed"


def test_workspace_routes_refuse_bad_bodies(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        assert client.post(_url("claim"), content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
        r = client.post(_url("claim"), content=b"{not json", headers={"Content-Type": "application/json"})
        assert (r.status_code, r.json()["error"]) == (400, "invalid_json")
        r = client.post(_url("claim"), json=[1, 2])
        assert (r.status_code, r.json()["error"]) == (422, "invalid_state")
        r = client.post(_url("claim"), json={"seq": 1})
        assert (r.status_code, r.json()["error"]) == (422, "invalid_state")
        big = b'{"windowId":"w-A"' + b" " * MAX_BODY_BYTES + b"}"
        r = client.put(_url("active"), content=big, headers={"Content-Type": "application/json"})
        assert (r.status_code, r.json()["error"]) == (413, "too_large")


def test_workspace_routes_stream_without_window_is_204(tmp_path: Path):
    with TestClient(_app(tmp_path)) as client:
        assert client.get(_url("events")).status_code == 204
        assert client.delete(_url("active")).status_code == 204


async def test_workspace_routes_stream_delivers_inactive_and_ends_on_a_claim(tmp_path: Path):
    hub = hubs.get_hub()
    hub.claim("w-A", 41, True, _state("$sample"))
    app = _app(tmp_path)
    stream = Exchange(app, "GET", _url("events?window=w-A"), None, None)
    try:
        await stream.until(want_events=1)
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert hub.connected
        hub.claim("w-B", 1, True, _state("$empty"))
        await stream.until()
        assert stream.done
        assert [e["event"] for e in stream.events] == ["hello", "inactive"]
        assert stream.events[1]["data"] == {}
    finally:
        await stream.close()


async def test_workspace_routes_stream_client_gone_disconnects_the_window(tmp_path: Path):
    hub = hubs.get_hub()
    hub.claim("w-A", 41, True, _state("$sample"))
    stream = Exchange(_app(tmp_path), "GET", _url("events?window=w-A"), None, None)
    await stream.until(want_events=1)
    assert hub.document()["connected"] is True
    await stream.close()
    assert hub.document()["connected"] is False


async def test_workspace_routes_stream_ends_when_the_hub_closes(tmp_path: Path):
    hub = hubs.get_hub()
    hub.claim("w-A", 41, True, _state("$sample"))
    stream = Exchange(_app(tmp_path), "GET", _url("events?window=w-A"), None, None)
    try:
        await stream.until(want_events=1)
        hubs.reset_hub()
        await stream.until()
        assert stream.done
    finally:
        await stream.close()


async def test_workspace_routes_stream_ends_on_the_exit_signal(tmp_path: Path):
    hub = hubs.get_hub()
    hub.claim("w-A", 41, True, _state("$sample"))
    stream = Exchange(_app(tmp_path), "GET", _url("events?window=w-A"), None, None)
    try:
        await stream.until(want_events=1)
        AppStatus.should_exit = True
        await stream.until(timeout=3.0)
        # The exit signal cancels the response (no closing chunk); the window is let go.
        assert stream.task.done()
        assert hub.document()["connected"] is False
    finally:
        await stream.close()


async def test_workspace_routes_exit_watch_wakes_a_waiting_set_workspace():
    hub = Hub(epoch="e1")
    hub.claim("w-A", 41, True, _state("$sample"))
    hub.connect("w-A")
    async with exit_watch(lambda: hub):
        waiting = asyncio.create_task(hub.submit([{"cmd": "close_all", "args": {}}], wait_s=10))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        AppStatus.should_exit = True
        result = await asyncio.wait_for(waiting, 2.0)
    assert result["batch"]["status"] == "pending"
    assert hub.closed


def test_workspace_routes_lifespan_shutdown_resets_the_hub(tmp_path: Path):
    with TestClient(_app(tmp_path)):
        hub = hubs.get_hub()
    assert hub.closed
    assert hubs.get_hub() is not hub


# --- the opt-in -----------------------------------------------------------------------------


def test_workspace_routes_opt_in_is_off_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CANOPY_WORKSPACE", raising=False)
    s = Settings(drafts_dir=tmp_path)
    assert s.workspace is False
    assert "workspace" not in library_config(s)


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("true", True), ("0", False), ("", False)])
def test_workspace_routes_opt_in_reads_canopy_workspace(monkeypatch, tmp_path: Path, value: str, expected: bool):
    monkeypatch.setenv("CANOPY_WORKSPACE", value)
    s = Settings(drafts_dir=tmp_path)
    assert s.workspace is expected
    assert ("workspace" in library_config(s)) is expected


def test_workspace_routes_opt_in_on_advertises_path_and_protocol(tmp_path: Path):
    config = library_config(_settings(tmp_path, True))
    assert config["workspace"] == {"path": "workspace", "protocol": 1}


def test_workspace_routes_opt_in_off_registers_nothing(tmp_path: Path):
    app = _app(tmp_path, workspace=False)
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/workspace")]
    with TestClient(app) as client:
        for path in ("", "/", "/claim", "/active", "/events?window=w-A", "/active/w-A/result"):
            for method in ("GET", "POST", "PUT", "DELETE"):
                assert client.request(method, f"/workspace{path}").status_code == 404, (method, path)
