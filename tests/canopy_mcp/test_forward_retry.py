"""``CANOPY_RELAY_CONNECT_RETRY_S``: how long a relay waits for an upstream that is not up yet.

The upstream is a real ``websockets`` server. "Late" means it starts
listening on its port only after the browser's connection attempt began.
"""

import time
import socket
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import Server, ServerConnection, serve

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

PATH = "/hare/v1/ws/pvs"
LATE_BY_S = 0.6


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _greet(ws: ServerConnection) -> None:
    ws.send("welcome")
    for _ in ws:
        pass


class LateUpstream:
    """A ``websockets`` server that binds ``port`` only ``delay`` seconds after ``start``."""

    def __init__(self, port: int, delay: float) -> None:
        self.port, self.delay = port, delay
        self.server: Server | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        time.sleep(self.delay)
        self.server = serve(_greet, "127.0.0.1", self.port)
        self.server.serve_forever()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._thread.join(timeout=self.delay + 5)
        if self.server is not None:
            self.server.shutdown()
        self._thread.join(timeout=5)


def _client(tmp_path: Path, port: int, **overrides: object) -> TestClient:
    # The test client names its server "testserver" in Host, which the relay must accept.
    settings = Settings(
        base_url=f"http://127.0.0.1:{port}", drafts_dir=tmp_path / "drafts", self_url="http://testserver", **overrides
    )
    return TestClient(create_app(settings, startup_hooks=[]))


def _refused(client: TestClient) -> tuple[int, float]:
    """Connect to the relay; the close code the handshake was refused with, and how long it took."""
    started = time.monotonic()
    with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect(PATH) as ws:
        ws.receive_text()
    return closed.value.code, time.monotonic() - started


def test_retry_is_zero_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_RELAY_CONNECT_RETRY_S", raising=False)
    assert Settings().relay_connect_retry_s == 0


@pytest.mark.parametrize(("value", "expected"), [("3", 3.0), ("0.5", 0.5), ("", 0.0)])
def test_retry_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str, expected: float) -> None:
    monkeypatch.setenv("CANOPY_RELAY_CONNECT_RETRY_S", value)
    assert Settings().relay_connect_retry_s == expected


def test_a_negative_retry_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(relay_connect_retry_s=-1)


def test_retry_3_reaches_a_late_upstream(tmp_path: Path) -> None:
    upstream = LateUpstream(_free_port(), LATE_BY_S)
    with _client(tmp_path, upstream.port, relay_connect_retry_s=3) as client:
        upstream.start()
        try:
            started = time.monotonic()
            with client.websocket_connect(PATH) as ws:
                assert ws.receive_text() == "welcome"
            assert time.monotonic() - started < 3
        finally:
            upstream.stop()


def test_retry_0_closes_1011_without_waiting(tmp_path: Path) -> None:
    upstream = LateUpstream(_free_port(), LATE_BY_S)
    with _client(tmp_path, upstream.port) as client:
        upstream.start()
        try:
            code, elapsed = _refused(client)
        finally:
            upstream.stop()
    assert code == 1011
    assert elapsed < LATE_BY_S  # one attempt: it did not wait for the upstream


def test_retry_gives_up_with_1011_when_the_upstream_never_comes(tmp_path: Path) -> None:
    with _client(tmp_path, _free_port(), relay_connect_retry_s=0.4) as client:
        code, elapsed = _refused(client)
    assert code == 1011
    assert 0.4 <= elapsed < 3


def test_retry_does_not_delay_an_upstream_that_is_up(tmp_path: Path) -> None:
    upstream = LateUpstream(_free_port(), 0)
    upstream.start()
    deadline = time.monotonic() + 5
    while upstream.server is None and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        with _client(tmp_path, upstream.port, relay_connect_retry_s=3) as client:
            started = time.monotonic()
            with client.websocket_connect(PATH) as ws:
                assert ws.receive_text() == "welcome"
            assert time.monotonic() - started < 1
    finally:
        upstream.stop()


def test_an_upstream_that_rejects_the_handshake_is_not_retried(tmp_path: Path) -> None:
    """A listener that answers but is not a WebSocket server is final, not "not up yet"."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted: list[int] = []

    def answer() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(1)
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            conn.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        with _client(tmp_path, listener.getsockname()[1], relay_connect_retry_s=3) as client:
            code, elapsed = _refused(client)
    finally:
        listener.close()
        thread.join(timeout=5)
    assert code == 1011
    assert elapsed < 2
    assert len(accepted) == 1
