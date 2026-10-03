"""canopy_mcp settings, the HTTP client factory, the entry point and the live-test gate."""

import os
import sys
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from canopy_mcp import __main__ as entry
from canopy_mcp.client import API_KEY_HEADER, make_client
from canopy_mcp.config import Settings, get_settings

ENV_VARS = (
    "CANOPY_BASE_URL",
    "CANOPY_API_KEY",
    "CANOPY_DRAFTS_DIR",
    "CANOPY_VIEWER_DIST",
    "CANOPY_PANEL_ID",
    "CANOPY_PV_MODE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Settings ---------------------------------------------------------------


def test_settings_read_every_variable_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_BASE_URL", "http://canopy-api:8000")
    monkeypatch.setenv("CANOPY_API_KEY", "secret-read")
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("CANOPY_VIEWER_DIST", str(tmp_path / "viewer"))
    monkeypatch.setenv("CANOPY_PANEL_ID", "canopy")
    monkeypatch.setenv("CANOPY_PV_MODE", "mock")

    s = Settings()

    assert s.base_url == "http://canopy-api:8000"
    assert s.api_key == "secret-read"
    assert s.drafts_dir == tmp_path / "drafts"
    assert s.viewer_dist == tmp_path / "viewer"
    assert s.panel_id == "canopy"
    assert s.pv_mode == "mock"


def test_settings_defaults():
    s = Settings()
    assert s.pv_mode == "hare"
    assert s.panel_id is None
    assert s.api_key == ""
    assert isinstance(s.drafts_dir, Path)


def test_pv_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("CANOPY_PV_MODE", "epics")
    with pytest.raises(ValidationError):
        Settings()


def test_backend_key_registry_is_not_the_client_key(monkeypatch):
    # tests/conftest.py seeds CANOPY_API_KEYS (plural) for the backend; the
    # client key is CANOPY_API_KEY and must not pick it up.
    monkeypatch.setenv("CANOPY_API_KEYS", "a:read,b:read+write")
    assert Settings().api_key == ""


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("CANOPY_PANEL_ID", "first")
    first = get_settings()
    monkeypatch.setenv("CANOPY_PANEL_ID", "second")
    assert get_settings() is first
    assert get_settings().panel_id == "first"


# --- client factory ---------------------------------------------------------


async def test_client_uses_base_url_and_api_key_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "healthy"})

    settings = Settings(base_url="http://canopy.test:9000", api_key="k-read")
    async with make_client(settings, transport=httpx.MockTransport(handler)) as client:
        assert isinstance(client, httpx.AsyncClient)
        resp = await client.get("/health")

    assert resp.json() == {"status": "healthy"}
    assert str(seen[0].url) == "http://canopy.test:9000/health"
    assert seen[0].headers[API_KEY_HEADER] == "k-read"
    assert "k-read" not in str(seen[0].url)


def test_client_defaults_to_get_settings(monkeypatch):
    monkeypatch.setenv("CANOPY_BASE_URL", "http://from-env:8000")
    monkeypatch.setenv("CANOPY_API_KEY", "env-key")
    client = make_client()
    assert str(client.base_url) == "http://from-env:8000"
    assert client.headers[API_KEY_HEADER] == "env-key"


def test_client_sends_no_key_header_when_unset():
    client = make_client(Settings(api_key=""))
    assert API_KEY_HEADER not in client.headers


def test_client_merges_caller_headers():
    client = make_client(Settings(api_key="k"), headers={"Accept": "application/json"})
    assert client.headers[API_KEY_HEADER] == "k"
    assert client.headers["Accept"] == "application/json"


# --- entry point ------------------------------------------------------------


def test_cli_defaults_bind_to_loopback():
    args = entry.parse_args(["--http"])
    assert args.http is True
    assert args.host == "127.0.0.1"
    assert isinstance(args.port, int)


def test_cli_accepts_host_and_port():
    args = entry.parse_args(["--http", "--host", "0.0.0.0", "--port", "9123"])
    assert (args.host, args.port) == ("0.0.0.0", 9123)


def test_stdio_is_refused_with_clear_message(capsys, monkeypatch):
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.append((a, k)))

    code = entry.main([])

    assert code != 0
    assert ran == []
    err = capsys.readouterr().err
    assert "stdio" in err
    assert "--http" in err


def test_http_serves_the_full_app_not_bare_fastmcp(monkeypatch):
    """``--http`` must serve create_app (viewer, drafts, forward, /health, proxy guard)."""
    import canopy_mcp.app as app_mod

    built = []
    sentinel = object()

    def fake_create_app(*args, **kwargs):
        built.append((args, kwargs))
        return sentinel

    calls = []
    monkeypatch.setattr(app_mod, "create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("canopy_mcp.server.mcp.run", lambda *a, **k: pytest.fail("bare FastMCP run"))

    assert entry.main(["--http", "--host", "0.0.0.0", "--port", "9124"]) == 0
    assert built == [((), {"self_url": "http://127.0.0.1:9124"})]
    assert calls == [
        ((sentinel,), {"host": "0.0.0.0", "port": 9124, "ws_per_message_deflate": False})
    ]


# --- server -----------------------------------------------------------------


def test_server_exposes_named_fastmcp_and_register_all():
    from fastmcp import FastMCP

    from canopy_mcp.server import mcp, register_all

    assert isinstance(mcp, FastMCP)
    assert mcp.name == "canopy"
    assert register_all() is mcp


# --- live-test gate ---------------------------------------------------------

_LIVE_CONFTEST = Path(__file__).with_name("conftest.py").read_text()
_LIVE_TESTS = "import pytest\n@pytest.mark.live\ndef test_live(): pass\ndef test_plain(): pass\n"


def _run_isolated_session(tmp_path: Path, live: str | None) -> str:
    """Run this directory's conftest against a tiny suite in a fresh pytest process."""
    (tmp_path / "conftest.py").write_text(_LIVE_CONFTEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    live: live stack\n")
    (tmp_path / "test_gate.py").write_text(_LIVE_TESTS)
    env = {k: v for k, v in os.environ.items() if k != "CANOPY_LIVE"}
    if live is not None:
        env["CANOPY_LIVE"] = live
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_live_marker_is_skipped_without_canopy_live(tmp_path):
    out = _run_isolated_session(tmp_path, live=None)
    assert "1 passed, 1 skipped" in out
    assert "CANOPY_LIVE=1" in out


def test_live_marker_runs_with_canopy_live(tmp_path):
    out = _run_isolated_session(tmp_path, live="1")
    assert "2 passed" in out
