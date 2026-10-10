"""The screenshot browser's lifecycle: lazy launch, the one lock, the idle stop, the bounded stop and the kill.

The live tests need a Chromium from ``playwright install chromium`` and fail,
not skip, without one.
"""

import os
import sys
import time
import signal
import asyncio
import subprocess
from types import SimpleNamespace
from pathlib import Path

import psutil
import pytest
from starlette.testclient import TestClient

from canopy_mcp import browser as browser_mod
from canopy_mcp.app import create_app, chromium_hook
from canopy_mcp.config import Settings
from canopy_mcp.browser import (
    UNAVAILABLE,
    Browser,
    BrowserNotStarted,
    get_browser,
    browser_hook,
    kill_process_tree,
    redirect_refusal_supported,
)


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


@pytest.fixture
def fast_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_mod, "STOP_TIMEOUT_S", 0.3)


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


# --- Nothing launches at startup --------------------------------------------------


def test_the_hook_installs_a_browser_without_launching_it(tmp_path: Path) -> None:
    app = create_app(
        Settings(drafts_dir=tmp_path / "drafts"), startup_hooks=[chromium_hook], self_url="http://127.0.0.1:9"
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        browser = app.state.browser
        assert isinstance(browser, Browser)
        assert not browser.running
        assert browser._playwright is None and browser._browser is None
        assert get_browser() is browser
        assert browser.base_url == "http://127.0.0.1:9"
        assert browser.access_token is None and browser.sandbox is False
    with pytest.raises(BrowserNotStarted, match="unavailable"):
        get_browser()


def test_the_hook_passes_the_credential_and_the_sandbox_switch(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "front.access"
    token_file.write_text("hook-token-41\n")
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "1")
    settings = Settings(drafts_dir=tmp_path / "drafts", access_token_file=token_file)
    assert settings.browser_sandbox is True
    app = create_app(settings, startup_hooks=[chromium_hook])
    with TestClient(app):
        browser = app.state.browser
        assert browser.access_token == "hook-token-41"
        assert browser.sandbox is True
        assert not browser.running


async def test_the_hook_exit_stops_under_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def fake_stop(self: Browser) -> None:
        events.append(f"stop locked={self.lock.locked()}")

    monkeypatch.setattr(Browser, "stop", fake_stop)
    app = SimpleNamespace(state=SimpleNamespace())
    async with browser_hook("http://127.0.0.1:1", access_token="t")(app) as browser:
        assert app.state.browser is browser and get_browser() is browser
        assert browser.access_token == "t"
        assert events == []
    assert events == ["stop locked=True"]
    with pytest.raises(BrowserNotStarted):
        get_browser()


# --- Settings -----------------------------------------------------------------------


def test_browser_sandbox_defaults_off_and_empty_counts_as_unset(monkeypatch) -> None:
    monkeypatch.delenv("CANOPY_BROWSER_SANDBOX", raising=False)
    assert Settings().browser_sandbox is False
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "")
    assert Settings().browser_sandbox is False
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "0")
    assert Settings().browser_sandbox is False
    monkeypatch.setenv("CANOPY_BROWSER_SANDBOX", "1")
    assert Settings().browser_sandbox is True


# --- Unavailable ---------------------------------------------------------------------


@pytest.fixture
def no_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == "playwright" or name.startswith("playwright.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "playwright", None)


async def test_without_playwright_the_capture_is_unavailable(no_playwright: None, caplog) -> None:
    browser = Browser("http://127.0.0.1:1")
    with caplog.at_level("WARNING", logger="canopy_mcp.browser"), pytest.raises(BrowserNotStarted) as info:
        async with browser.context():
            pass
    assert str(info.value).startswith(UNAVAILABLE)
    assert "playwright is not installed" in str(info.value)
    assert "screenshots are unavailable" in caplog.text
    assert not browser.running and not browser.lock.locked()


async def test_without_the_redirect_hook_the_capture_is_unavailable(monkeypatch) -> None:
    assert redirect_refusal_supported()
    monkeypatch.setattr(browser_mod, "redirect_refusal_supported", lambda: False)
    browser = Browser("http://127.0.0.1:1")
    with pytest.raises(BrowserNotStarted, match="websockets>=14"):
        await browser.start()
    assert not browser.running


async def test_a_chromium_that_does_not_launch_is_unavailable(monkeypatch, chromium: None) -> None:
    async def no_chromium(playwright, *, sandbox):
        raise RuntimeError("Executable doesn't exist at /nowhere/chrome\nLooks like Playwright was just installed")

    monkeypatch.setattr(browser_mod, "_launch_chromium", no_chromium)
    browser = Browser("http://127.0.0.1:1")
    with pytest.raises(BrowserNotStarted) as info:
        await browser.start()
    message = str(info.value)
    assert message.startswith(UNAVAILABLE)
    assert "Executable doesn't exist at /nowhere/chrome" in message
    assert "Looks like" not in message  # first line only
    assert "CANOPY_BROWSER_SANDBOX" not in message
    assert browser._playwright is None and browser._browser is None and not browser.running


async def test_a_sandbox_launch_that_fails_names_the_switch(monkeypatch, chromium: None) -> None:
    seen: list[bool] = []

    async def no_sandbox(playwright, *, sandbox):
        seen.append(sandbox)
        raise RuntimeError("Failed to launch: No usable sandbox!")

    monkeypatch.setattr(browser_mod, "_launch_chromium", no_sandbox)
    browser = Browser("http://127.0.0.1:1", sandbox=True)
    with pytest.raises(BrowserNotStarted, match="CANOPY_BROWSER_SANDBOX"):
        await browser.start()
    assert seen == [True]
    assert not browser.running


async def test_the_sandbox_switch_reaches_the_launch_options(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeChromium:
        async def launch(self, **options):
            calls.append(options)
            raise RuntimeError("stop here")

    fake = SimpleNamespace(chromium=FakeChromium())
    with pytest.raises(RuntimeError):
        await browser_mod._launch_chromium(fake, sandbox=False)
    with pytest.raises(RuntimeError):
        await browser_mod._launch_chromium(fake, sandbox=True)
    assert calls == [{"headless": True}, {"headless": True, "chromium_sandbox": True}]


def test_get_browser_without_an_installed_browser_is_unavailable() -> None:
    browser_mod.set_browser(None)
    with pytest.raises(BrowserNotStarted, match="unavailable"):
        get_browser()


# --- Launch, lock, idle -----------------------------------------------------------------


async def test_the_first_capture_launches_and_the_idle_task_stops(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1", idle_timeout_s=0.4)
    try:
        assert not browser.running
        async with browser.context() as context:
            assert browser.running
            assert browser._idle_task is not None and not browser._idle_task.done()
            page = await context.new_page()
            assert await page.evaluate("1 + 1") == 2
        assert browser.running  # the capture ended; the idle task has not fired yet
        assert await _wait_until(lambda: not browser.running, timeout=5)
        assert browser._playwright is None and browser._browser is None and browser._idle_task is None
        assert browser._driver_pid is None and browser._driver_children == []
    finally:
        await browser.shutdown()


async def test_the_idle_stop_waits_for_an_in_flight_capture(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1", idle_timeout_s=0.3)
    try:
        async with browser.context():
            await asyncio.sleep(0.8)  # well past the idle timeout, under the lock
            assert browser.running
        assert await _wait_until(lambda: not browser.running, timeout=5)
    finally:
        await browser.shutdown()


async def test_a_capture_after_the_idle_stop_relaunches(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1", idle_timeout_s=0.3)
    try:
        async with browser.context():
            first = browser._browser
        assert await _wait_until(lambda: not browser.running, timeout=5)
        async with browser.context():
            assert browser.running and browser._browser is not first
    finally:
        await browser.shutdown()


async def test_captures_are_serialised(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1")
    spans: list[tuple[str, float, float]] = []

    async def one(name: str) -> None:
        async with browser.context():
            started = time.monotonic()
            await asyncio.sleep(0.25)
            spans.append((name, started, time.monotonic()))

    try:
        await asyncio.gather(one("a"), one("b"), one("c"))
    finally:
        await browser.shutdown()
    assert len(spans) == 3
    spans.sort(key=lambda span: span[1])
    for (_, _, end), (_, start, _) in zip(spans, spans[1:]):
        assert start >= end, spans  # no two captures overlap


async def test_start_and_stop_are_idempotent_and_the_snapshot_is_taken(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1")
    try:
        await browser.start()
        first = browser._browser
        pid = browser._driver_pid
        assert pid is not None and psutil.pid_exists(pid)
        assert isinstance(browser._driver_children, list)
        await browser.start()
        assert browser._browser is first
        await browser.stop()
        assert not browser.running and browser._driver_pid is None
        await browser.stop()
        assert await _wait_until(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == "zombie")
    finally:
        await browser.shutdown()


async def test_shutdown_waits_for_a_capture_in_flight(chromium: None) -> None:
    browser = Browser("http://127.0.0.1:1")
    order: list[str] = []

    async def capture() -> None:
        async with browser.context():
            order.append("capture-start")
            await asyncio.sleep(0.4)
            order.append("capture-end")

    async def shut() -> None:
        await asyncio.sleep(0.1)
        await browser.shutdown()
        order.append("shutdown")

    await asyncio.gather(capture(), shut())
    assert order == ["capture-start", "capture-end", "shutdown"]
    assert not browser.running


# --- Crash recovery --------------------------------------------------------------------


async def test_a_crashed_chromium_is_replaced_on_the_next_capture(chromium: None, caplog) -> None:
    browser = Browser("http://127.0.0.1:1")
    try:
        await browser.start()
        chromium_pids = [proc.pid for proc in browser._driver_children]
        assert chromium_pids
        for pid in chromium_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert await _wait_until(lambda: not browser.running, timeout=10)
        with caplog.at_level("WARNING", logger="canopy_mcp.browser"):
            async with browser.context() as context:
                assert browser.running
                page = await context.new_page()
                assert await page.evaluate("2 + 2") == 4
        assert "relaunching" in caplog.text
    finally:
        await browser.shutdown()


# --- Bounded stop and the kill -------------------------------------------------------------


class _HangingBrowser:
    def __init__(self) -> None:
        self.close_calls = 0

    def is_connected(self) -> bool:
        return True

    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.Event().wait()


class _Playwright:
    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self) -> None:
        self.stopped += 1


def _fake_running(browser: Browser, pid: int = 4242) -> tuple[_HangingBrowser, _Playwright]:
    hanging, playwright = _HangingBrowser(), _Playwright()
    browser._browser = hanging  # type: ignore[assignment]
    browser._playwright = playwright  # type: ignore[assignment]
    browser._driver_pid = pid
    browser._driver_children = ["child-a", "child-b"]  # type: ignore[list-item]
    return hanging, playwright


async def test_stop_returns_within_the_bound_and_calls_the_kill(fast_stop: None, monkeypatch, caplog) -> None:
    kills: list[tuple[int, list]] = []
    monkeypatch.setattr(browser_mod, "kill_process_tree", lambda pid, children: kills.append((pid, list(children))))
    browser = Browser("http://127.0.0.1:1")
    hanging, playwright = _fake_running(browser)
    started = time.monotonic()
    with caplog.at_level("WARNING", logger="canopy_mcp.browser"):
        await browser.stop()
    assert time.monotonic() - started < 2.0
    assert hanging.close_calls == 1
    assert playwright.stopped == 0  # a driver that hangs on close is killed, not asked again
    assert kills == [(4242, ["child-a", "child-b"])]
    assert browser._browser is None and browser._playwright is None and not browser.running
    assert browser._driver_pid is None and browser._driver_children == []
    assert "did not finish within" in caplog.text


async def test_a_hanging_playwright_stop_is_killed_too(fast_stop: None, monkeypatch) -> None:
    kills: list[int] = []
    monkeypatch.setattr(browser_mod, "kill_process_tree", lambda pid, children: kills.append(pid))

    class QuickBrowser:
        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    class HangingPlaywright:
        async def stop(self) -> None:
            await asyncio.Event().wait()

    browser = Browser("http://127.0.0.1:1")
    browser._browser = QuickBrowser()  # type: ignore[assignment]
    browser._playwright = HangingPlaywright()  # type: ignore[assignment]
    browser._driver_pid = 777
    await browser.stop()
    assert kills == [777]
    assert not browser.running and browser._playwright is None


async def test_a_kill_that_raises_still_lets_the_next_capture_relaunch(
    fast_stop: None, monkeypatch, caplog, chromium: None
) -> None:
    def failing_kill(pid, children):
        raise PermissionError("not allowed")

    monkeypatch.setattr(browser_mod, "kill_process_tree", failing_kill)
    browser = Browser("http://127.0.0.1:1")
    _fake_running(browser)
    with caplog.at_level("ERROR", logger="canopy_mcp.browser"):
        await browser.stop()
    assert "killing the driver" in caplog.text
    assert browser._browser is None and browser._playwright is None and not browser.running
    try:
        async with browser.context() as context:
            assert browser.running
            page = await context.new_page()
            assert await page.evaluate("3 + 3") == 6
    finally:
        await browser.shutdown()


async def test_a_timeout_with_an_unknown_driver_pid_still_drops_the_references(
    fast_stop: None, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(browser_mod, "kill_process_tree", lambda pid, children: pytest.fail("no pid to kill"))
    browser = Browser("http://127.0.0.1:1")
    _fake_running(browser)
    browser._driver_pid = None
    with caplog.at_level("ERROR", logger="canopy_mcp.browser"):
        await browser.stop()
    assert "driver pid unknown" in caplog.text
    assert not browser.running and browser._browser is None


async def test_the_idle_task_is_cancelled_by_stop(fast_stop: None) -> None:
    browser = Browser("http://127.0.0.1:1", idle_timeout_s=60)
    browser._browser = _HangingBrowser()  # type: ignore[assignment]
    browser._idle_task = asyncio.create_task(browser._stop_when_idle())
    await asyncio.sleep(0)
    idle = browser._idle_task
    await browser.stop()  # no driver pid: nothing to kill, the references are still dropped
    assert await _wait_until(idle.done)
    assert idle.cancelled()
    assert browser._idle_task is None


def test_kill_process_tree_removes_a_real_parent_and_child_pair() -> None:
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline())  # type: ignore[union-attr]
        snapshot = psutil.Process(parent.pid).children(recursive=True)
        assert child_pid in [proc.pid for proc in snapshot]
        kill_process_tree(parent.pid, snapshot)
        # psutil reaps our own child inside wait_procs, so Popen.wait() sees ECHILD (reported as 0):
        # the evidence is the pid being gone, not the returncode.
        parent.wait(timeout=5)
        assert not psutil.pid_exists(parent.pid)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(child_pid) and psutil.Process(child_pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "the child survived the kill"
            time.sleep(0.02)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except (ProcessLookupError, NameError):
            pass


def test_kill_process_tree_tolerates_processes_that_are_already_gone() -> None:
    """A driver that died on its own, or a snapshotted child that already exited, is skipped, not raised."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=10)  # reaped: the pid is free
    ghost = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ghost_proc = psutil.Process(ghost.pid)
    ghost.kill()
    ghost.wait(timeout=5)
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        kill_process_tree(gone.pid, [ghost_proc, psutil.Process(alive.pid)])
        alive.wait(timeout=5)  # reaped by psutil already; see the test above
        assert not psutil.pid_exists(alive.pid)
    finally:
        if alive.poll() is None:
            alive.kill()
            alive.wait(timeout=5)
