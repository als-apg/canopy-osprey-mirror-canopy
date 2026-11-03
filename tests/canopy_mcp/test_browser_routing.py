"""The screenshot browser's origin lock: HTTP routing, the WebSocket relay and the credential.

A recording upstream (a Starlette app on a real port) is the browser's own
origin; a second instance on another port is "off-origin". Every test drives
a page served by the first and checks what each upstream received. These
tests need a Chromium from ``playwright install chromium`` and fail, not
skip, without one.
"""

import time
import socket
import asyncio
import threading
from pathlib import Path
from collections.abc import Iterator

import pytest
import uvicorn
from starlette.routing import Route, WebSocketRoute
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import (
    Response,
    HTMLResponse,
    RedirectResponse,
    PlainTextResponse,
)
from starlette.websockets import WebSocket, WebSocketDisconnect
from starlette.applications import Starlette

from canopy_mcp import browser as browser_mod
from canopy_mcp.app import create_app
from canopy_mcp.png import PNG_SIGNATURE
from canopy_mcp.access import ACCESS_HEADER
from canopy_mcp.config import Settings
from canopy_mcp.browser import Browser, origin_of, same_origin
from canopy_mcp.tools.screenshot import capture

TOKEN = "browser-credential-9c1e"
HEADER = ACCESS_HEADER.lower()
ALL_METHODS = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8"><title>origin page</title></head>
<body>
<script>
window.__fetch = async (url, method) => {
  try {
    const body = (method === 'GET' || method === 'HEAD') ? undefined : 'x';
    const response = await fetch(url, { method, body });
    return { ok: true, status: response.status };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
};
window.__ws = (url, send, wanted) => new Promise((resolve) => {
  const out = { messages: [], code: null, error: false, construct: null };
  let ws;
  try { ws = new WebSocket(url); } catch (error) { out.construct = String(error); resolve(out); return; }
  ws.onmessage = (event) => {
    out.messages.push(event.data);
    if (send && out.messages.length === 1) ws.send(send);
    if (out.messages.length >= wanted) ws.close(1000, 'done');
  };
  ws.onerror = () => { out.error = true; };
  ws.onclose = (event) => { out.code = event.code; resolve(out); };
  setTimeout(() => { try { ws.close(); } catch (e) {} resolve(out); }, 8000);
});
window.__open_ws = (url) => { window.__sock = new WebSocket(url); return new Promise((r) => { window.__sock.onopen = () => r(true); window.__sock.onclose = () => r(false); }); };
window.__worker = (wsUrl, fetchUrl) => new Promise((resolve) => {
  const code = `self.onmessage = async (e) => {
    const out = { fetch: null, ws: [] };
    try { const r = await fetch(e.data.fetchUrl); out.fetch = { ok: true, status: r.status }; }
    catch (error) { out.fetch = { ok: false, error: String(error) }; }
    const ws = new WebSocket(e.data.wsUrl);
    ws.onopen = () => out.ws.push('open');
    ws.onmessage = (m) => { out.ws.push('message:' + m.data); ws.close(1000); };
    ws.onerror = () => out.ws.push('error');
    ws.onclose = (c) => { out.ws.push('close:' + c.code); postMessage(out); };
  };`;
  const worker = new Worker(URL.createObjectURL(new Blob([code], { type: 'application/javascript' })));
  worker.onmessage = (e) => resolve(e.data);
  worker.postMessage({ wsUrl, fetchUrl });
  setTimeout(() => resolve({ fetch: 'timeout', ws: ['timeout'] }), 8000);
});
</script>
</body></html>
"""

STUB_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /><title>stub viewer</title></head>
<body style="margin:0"><div id="root"><div data-testid="screen-root" style="width:200px;height:100px;background:#08c"></div></div>
<script>window.__canopy = { pvStatus: 'mock' };</script></body></html>
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Recorder:
    """A Starlette app on a real port that records every request and WebSocket handshake it receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.handshakes: list[tuple[str, dict[str, list[str]]]] = []
        self.open_sockets = 0
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}"
        self.app = Starlette(
            routes=[
                Route("/page", self.page, methods=["GET"]),
                Route("/echo", self.echo, methods=ALL_METHODS),
                Route("/redirect/{code:int}", self.redirect, methods=["GET"]),
                Route("/not-modified", self.not_modified, methods=["GET"]),
                Route("/multiple-choices", self.multiple_choices, methods=["GET"]),
                WebSocketRoute("/ws", self.ws),
                WebSocketRoute("/ws-redirect", self.ws_redirect),
            ]
        )
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @staticmethod
    def _headers(raw) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, value in raw:
            out.setdefault(name.decode().lower(), []).append(value.decode())
        return out

    def _record(self, request: Request) -> None:
        self.requests.append((request.method, request.url.path, self._headers(request.scope["headers"])))

    async def page(self, request: Request) -> Response:
        self._record(request)
        return HTMLResponse(PAGE)

    async def echo(self, request: Request) -> Response:
        self._record(request)
        return PlainTextResponse(f"{request.method} ok")

    async def redirect(self, request: Request) -> Response:
        self._record(request)
        return RedirectResponse(request.query_params["to"], status_code=request.path_params["code"])

    async def not_modified(self, request: Request) -> Response:
        self._record(request)
        return Response(status_code=304, headers={"etag": '"same"'})

    async def multiple_choices(self, request: Request) -> Response:
        self._record(request)
        return PlainTextResponse("pick one", status_code=300)

    async def ws(self, websocket: WebSocket) -> None:
        self.handshakes.append((websocket.url.path, self._headers(websocket.scope["headers"])))
        await websocket.accept()
        self.open_sockets += 1
        try:
            await websocket.send_text("welcome")
            while True:
                message = await websocket.receive_text()
                if message == "close-me":
                    await websocket.close(code=4000, reason="bye")
                    return
                await websocket.send_text(f"echo:{message}")
        except WebSocketDisconnect:
            pass
        finally:
            self.open_sockets -= 1

    async def ws_redirect(self, websocket: WebSocket) -> None:
        self.handshakes.append((websocket.url.path, self._headers(websocket.scope["headers"])))
        await websocket.send_denial_response(Response(status_code=302, headers={"location": f"{self.ws_url}/ws"}))

    def received(
        self, method: str | None = None, path: str | None = None
    ) -> list[tuple[str, str, dict[str, list[str]]]]:
        return [r for r in self.requests if (method is None or r[0] == method) and (path is None or r[1] == path)]

    def __enter__(self) -> "Recorder":
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("recording upstream did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.fixture(scope="module")
def chromium() -> None:
    if not _chromium_installed():
        pytest.fail("chromium not installed for this interpreter: run `python -m playwright install chromium`")


@pytest.fixture(scope="module")
def origin(chromium: None) -> Iterator[Recorder]:
    with Recorder() as recorder:
        yield recorder


@pytest.fixture(scope="module")
def other(chromium: None) -> Iterator[Recorder]:
    with Recorder() as recorder:
        yield recorder


@pytest.fixture
async def browser(origin: Recorder):
    browser = Browser(origin.base_url, access_token=TOKEN)
    try:
        yield browser
    finally:
        await browser.shutdown()


@pytest.fixture(autouse=True)
def fresh_records(request: pytest.FixtureRequest) -> None:
    for name in ("origin", "other"):
        if name in request.fixturenames:
            recorder = request.getfixturevalue(name)
            recorder.requests.clear()
            recorder.handshakes.clear()


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


# --- The origin predicate -----------------------------------------------------------


def test_origin_is_an_exact_scheme_host_port_tuple() -> None:
    assert origin_of("http://127.0.0.1:8321/x?y") == ("http", "127.0.0.1", 8321)
    assert origin_of("ws://127.0.0.1:8321/ws") == ("http", "127.0.0.1", 8321)
    assert origin_of("wss://Host.example/ws") == ("https", "host.example", 443)
    assert origin_of("http://host.example") == ("http", "host.example", 80)
    base = origin_of("http://127.0.0.1:8321")
    assert same_origin("http://127.0.0.1:8321/embed/drafts/a", base)
    assert same_origin("ws://127.0.0.1:8321/hare/v1/ws/pvs", base)
    assert not same_origin("http://127.0.0.1:8322/", base)
    assert not same_origin("https://127.0.0.1:8321/", base)
    assert not same_origin("http://localhost:8321/", base)
    assert not same_origin("http://127.0.0.1/", base)


@pytest.mark.parametrize(
    "spelling",
    [
        "http://127.0.0.1:80001/x",
        "http://127.0.0.1:8321@evil.example/x",
        "http://127.0.0.1:8321@evil.example:8321/x",
        "http://user@127.0.0.1:8321/x",
        "http://user:pw@127.0.0.1:8321/x",
        "ftp://127.0.0.1:8321/x",
        "http:///x",
        "data:text/plain,hi",
    ],
)
def test_malformed_port_and_userinfo_spellings_have_no_origin(spelling: str) -> None:
    assert origin_of(spelling) is None
    assert not same_origin(spelling, ("http", "127.0.0.1", 8321))


def test_nothing_is_same_origin_with_a_missing_origin() -> None:
    assert not same_origin("http://127.0.0.1:1/", None)


# --- HTTP routing -------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
async def test_off_origin_requests_are_aborted(
    browser: Browser, origin: Recorder, other: Recorder, method: str
) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{other.base_url}/echo", method])
    assert result["ok"] is False
    assert other.requests == []
    assert [(m, p) for m, p, _ in origin.requests] == [("GET", "/page")]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_same_origin_writes_are_aborted_before_any_network_call(
    browser: Browser, origin: Recorder, method: str
) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{origin.base_url}/echo", method])
    assert result["ok"] is False
    assert origin.received(method) == []
    assert origin.received(path="/echo") == []
    # The credential travelled only with the page itself.
    carrying = [(m, p) for m, p, headers in origin.requests if HEADER in headers]
    assert carrying == [("GET", "/page")]
    assert browser._last_session_for_tests is not None
    assert (
        method,
        f"http://127.0.0.1:{origin.port}",
        "/echo",
        "aborted: method",
    ) in browser._last_session_for_tests.records


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_same_origin_reads_pass_with_exactly_one_credential(
    browser: Browser, origin: Recorder, method: str
) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{origin.base_url}/echo", method])
    assert result == {"ok": True, "status": 200}
    (received,) = origin.received(method, "/echo")
    assert received[2][HEADER] == [TOKEN]
    assert "cookie" not in received[2]


async def test_a_page_cannot_forge_or_double_the_credential(browser: Browser, origin: Recorder) -> None:
    """A header the page sets itself is dropped; the server still sees exactly one, the browser's."""
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate(
            "([u, h]) => fetch(u, { headers: { [h]: 'forged' } }).then(r => r.status).catch(e => String(e))",
            [f"{origin.base_url}/echo", ACCESS_HEADER],
        )
    assert result == 200
    (received,) = origin.received("GET", "/echo")
    assert received[2][HEADER] == [TOKEN]


async def test_without_a_token_no_credential_header_is_sent(origin: Recorder) -> None:
    browser = Browser(origin.base_url)
    try:
        async with browser.context() as context:
            page = await context.new_page()
            await page.goto(f"{origin.base_url}/page", wait_until="load")
            result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{origin.base_url}/echo", "GET"])
    finally:
        await browser.shutdown()
    assert result == {"ok": True, "status": 200}
    (received,) = origin.received("GET", "/echo")
    assert HEADER not in received[2]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
async def test_same_origin_redirects_to_off_origin_are_aborted(
    browser: Browser, origin: Recorder, other: Recorder, code: int
) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        url = f"{origin.base_url}/redirect/{code}?to={other.base_url}/echo"
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [url, "GET"])
    assert result["ok"] is False
    assert other.requests == []
    (received,) = origin.received("GET", f"/redirect/{code}")
    assert received[2][HEADER] == [TOKEN]  # the one fetch, with the header, that was never followed
    assert browser._last_session_for_tests is not None
    assert ("GET", f"http://127.0.0.1:{origin.port}", f"/redirect/{code}", f"aborted: redirect {code}") in (
        browser._last_session_for_tests.records
    )


async def test_a_same_origin_redirect_is_not_followed_either(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        url = f"{origin.base_url}/redirect/302?to={origin.base_url}/echo"
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [url, "GET"])
    assert result["ok"] is False
    assert origin.received("GET", "/echo") == []


async def test_a_304_passes(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{origin.base_url}/not-modified", "GET"])
    assert result == {"ok": True, "status": 304}
    (received,) = origin.received("GET", "/not-modified")
    assert received[2][HEADER] == [TOKEN]
    assert browser._last_session_for_tests is not None
    assert (
        "GET",
        f"http://127.0.0.1:{origin.port}",
        "/not-modified",
        "fulfilled 304",
    ) in browser._last_session_for_tests.records


async def test_a_3xx_without_location_is_fulfilled(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("([u, m]) => window.__fetch(u, m)", [f"{origin.base_url}/multiple-choices", "GET"])
    assert result == {"ok": True, "status": 300}


@pytest.mark.parametrize(
    "spelling",
    [
        "http://127.0.0.1:80001/echo",
        "http://127.0.0.1:{port}@127.0.0.1:{other}/echo",
        "http://evil@127.0.0.1:{port}/echo",
    ],
)
async def test_malformed_port_and_userinfo_spellings_reach_no_upstream(
    browser: Browser, origin: Recorder, other: Recorder, spelling: str
) -> None:
    url = spelling.format(port=origin.port, other=other.port)
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        fetched = await page.evaluate("([u, m]) => window.__fetch(u, m)", [url, "GET"])
        # An <img> is the other way a page can spell a URL the fetch API refuses.
        await page.evaluate(
            "(u) => new Promise((r) => { const i = new Image(); i.onload = () => r('load'); i.onerror = () => r('error'); i.src = u; setTimeout(() => r('timeout'), 3000); })",
            url,
        )
    assert fetched["ok"] is False
    assert origin.received(path="/echo") == []
    assert other.requests == []


# --- WebSocket relay ----------------------------------------------------------------


async def test_off_origin_websocket_is_closed(browser: Browser, origin: Recorder, other: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("(u) => window.__ws(u, null, 1)", f"{other.ws_url}/ws")
    assert result["messages"] == []
    assert result["code"] not in (None, 1000)
    assert other.handshakes == []
    assert other.open_sockets == 0


async def test_relay_pumps_both_ways_with_the_credential(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("(u) => window.__ws(u, 'ping', 2)", f"{origin.ws_url}/ws")
    assert result["messages"] == ["welcome", "echo:ping"]
    assert result["code"] == 1000
    (handshake,) = origin.handshakes
    assert handshake[0] == "/ws"
    assert handshake[1][HEADER] == [TOKEN]
    assert "cookie" not in handshake[1]


async def test_upstream_close_reaches_the_page(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("(u) => window.__ws(u, 'close-me', 99)", f"{origin.ws_url}/ws")
    assert result["messages"] == ["welcome"]
    assert result["code"] == 4000


async def test_a_redirecting_upstream_gets_no_second_handshake_and_the_page_sees_1011(
    browser: Browser, origin: Recorder
) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("(u) => window.__ws(u, null, 1)", f"{origin.ws_url}/ws-redirect")
    assert result["messages"] == []
    assert result["code"] == 1011
    assert [path for path, _ in origin.handshakes] == ["/ws-redirect"]
    assert origin.handshakes[0][1][HEADER] == [TOKEN]


async def test_a_relay_error_closes_the_page_socket_with_1011(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate("(u) => window.__ws(u, null, 1)", f"{origin.ws_url}/no-such-socket")
    assert result["messages"] == []
    assert result["code"] == 1011


async def test_no_socket_remains_after_a_capture(browser: Browser, origin: Recorder) -> None:
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        assert await page.evaluate("(u) => window.__open_ws(u)", f"{origin.ws_url}/ws") is True
        session = browser._last_session_for_tests
        assert session is not None
        await _wait_until(lambda: origin.open_sockets == 1 and bool(session.upstreams))
        assert origin.open_sockets == 1
        assert session.tasks and session.upstreams and session.sockets
    await _wait_until(lambda: origin.open_sockets == 0)
    assert origin.open_sockets == 0
    assert session.tasks == set() and session.upstreams == set() and session.sockets == set()


async def test_a_dedicated_worker_is_locked_to_the_origin_too(
    browser: Browser, origin: Recorder, other: Recorder
) -> None:
    """Requests and sockets opened from a Worker go through the same lock: off-origin reaches nothing."""
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate(
            "([w, f]) => window.__worker(w, f)", [f"{other.ws_url}/ws", f"{other.base_url}/echo"]
        )
    assert result["fetch"]["ok"] is False
    assert "open" not in result["ws"] and result["ws"][-1].startswith("close:")
    assert other.requests == [] and other.handshakes == []


async def test_a_dedicated_worker_fetch_is_relayed_and_its_socket_fails_closed(
    browser: Browser, origin: Recorder
) -> None:
    """A Worker's fetch goes through the route handler with the credential.

    Playwright's WebSocket routing is page-scoped (1.63): a socket opened from
    a Worker is neither relayed nor let through directly -- it fails closed,
    and no handshake without the credential reaches the server. The viewer
    opens its PV socket from the page, so a capture is unaffected.
    """
    async with browser.context() as context:
        page = await context.new_page()
        await page.goto(f"{origin.base_url}/page", wait_until="load")
        result = await page.evaluate(
            "([w, f]) => window.__worker(w, f)", [f"{origin.ws_url}/ws", f"{origin.base_url}/echo"]
        )
    assert result["fetch"] == {"ok": True, "status": 200}
    (received,) = origin.received("GET", "/echo")
    assert received[2][HEADER] == [TOKEN]
    assert "open" not in result["ws"] and result["ws"][-1].startswith("close:")
    assert origin.handshakes == []  # nothing reached the server without the credential


def test_brief_redacts_the_credential_and_keeps_one_line() -> None:
    exc = RuntimeError(f"apiRequestContext.fetch: connect ECONNREFUSED\nCall log:\n  - {ACCESS_HEADER}: {TOKEN}")
    assert browser_mod._brief(exc, TOKEN) == "apiRequestContext.fetch: connect ECONNREFUSED"
    assert browser_mod._brief(RuntimeError(f"bad header {TOKEN} here"), TOKEN) == "bad header <redacted> here"
    assert browser_mod._brief(RuntimeError(""), TOKEN) == "RuntimeError"
    assert browser_mod._brief(RuntimeError("plain"), None) == "plain"


# --- The gated server serves its own browser ------------------------------------------


def _viewer(tmp: Path) -> Settings:
    drafts = tmp / "drafts"
    (drafts / "screens" / "xfer").mkdir(parents=True)
    (drafts / "screens" / "xfer" / "view.screen.json").write_text(
        '{"metadata": {"title": "XFER"}, "root": {"id": "root", "type": "absolute-canvas", "children": []}}'
    )
    dist = tmp / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(STUB_INDEX, encoding="utf-8")
    token_file = tmp / "front.access"
    token_file.write_text(TOKEN + "\n")
    return Settings(drafts_dir=drafts, viewer_dist=dist, pv_mode="mock", access_token_file=token_file)


class _GatedServer:
    def __init__(self, settings: Settings) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.app = create_app(settings, startup_hooks=[], self_url=self.base_url)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_GatedServer":
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("gated server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


async def test_a_gated_server_serves_its_own_screenshot_browser(chromium: None, tmp_path: Path) -> None:
    """The credential rides on the browser's requests, so a gated viewer renders; without it the gate answers 401."""
    with _GatedServer(_viewer(tmp_path)) as server:
        browser = Browser(server.base_url, access_token=TOKEN)
        try:
            data, info = await capture(browser, "xfer/view", settle_timeout_s=0.5)
        finally:
            await browser.shutdown()
        assert data.startswith(PNG_SIGNATURE)
        assert info == {"width": 200, "height": 100, "pv": "disconnected"}

        unauthenticated = Browser(server.base_url)
        try:
            with pytest.raises(ToolError, match="HTTP 401"):
                await capture(unauthenticated, "xfer/view", settle_timeout_s=0.5)
        finally:
            await unauthenticated.shutdown()


def test_the_hygiene_rules_hold_in_the_source() -> None:
    """No cookie and no context-wide header: the browser never calls either Playwright API."""
    source = Path(browser_mod.__file__).read_text(encoding="utf-8")
    assert "add_cookies" not in source
    assert "extra_http_headers" not in source
    assert "set_extra_http_headers" not in source
