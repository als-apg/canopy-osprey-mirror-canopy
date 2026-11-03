"""The server's ``/config.json`` names DATA's backend identity in hare mode.

The route asks DATA for ``GET /hare/v1/health/summary`` over ``app.state.http``
with a 1 s bound and adds ``backend {name, label, notice}`` and a top-level
``binding``. Every failure -- no shared client, a timeout, a transport error, a
non-200, a body that is not the summary -- gives both as null. Mock mode adds
neither key. No key or token value ever reaches the body.
"""

import time
import asyncio
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from canopy_mcp import static
from canopy_mcp.app import create_app
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from tests.canopy_mcp.conftest import summary

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
KEY = "sekrit-key"
SUMMARY = summary()

pytestmark = pytest.mark.real_data_summary


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key=KEY)


class Data:
    """A fake DATA: records every request and answers with ``respond``."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.respond(request)


def _json(body, status: int = 200):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return respond


def _config(settings: Settings, data: Data | None) -> tuple[httpx.Response, float]:
    """``GET /config.json`` with DATA replaced by ``data`` (``None``: the real client, unreachable host)."""
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        if data is not None:
            c.app.state.http = make_client(settings, transport=httpx.MockTransport(data))
        start = time.monotonic()
        r = c.get("/config.json")
        return r, time.monotonic() - start


def _nulls(body: dict) -> bool:
    return body["backend"] is None and body["binding"] is None


# --- success ---------------------------------------------------------------------


def test_hare_mode_names_the_backend_and_binding(settings: Settings) -> None:
    data = Data(_json(SUMMARY))
    r, _ = _config(settings, data)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == {"name": "sim", "label": "Simulator", "notice": None}
    assert body["binding"] == "va@3"
    assert "binding" not in body["backend"]


def test_the_summary_is_fetched_from_data_with_the_read_key(settings: Settings) -> None:
    data = Data(_json(SUMMARY))
    _config(settings, data)
    assert len(data.requests) == 1
    sent = data.requests[0]
    assert sent.method == "GET"
    assert sent.url.path == "/hare/v1/health/summary"
    assert sent.url.host == "127.0.0.1"
    assert sent.headers["x-api-key"] == KEY


@pytest.mark.parametrize("notice", ["fallback", "unservable"])
def test_notice_token_is_passed_through(settings: Settings, notice: str) -> None:
    r, _ = _config(settings, Data(_json({**SUMMARY, "notice": notice, "binding": notice})))
    body = r.json()
    assert body["backend"] == {"name": "sim", "label": "Simulator", "notice": notice}
    assert body["binding"] == notice


def test_a_summary_without_identity_gives_null_label_and_binding(settings: Settings) -> None:
    """A DATA whose connector has no identity (plain simulation) still names its backend."""
    bare = {k: v for k, v in SUMMARY.items() if k not in ("label", "notice", "binding")}
    bare["backend"] = "simulation"
    body = _config(settings, Data(_json(bare)))[0].json()
    assert body["backend"] == {"name": "simulation", "label": None, "notice": None}
    assert body["binding"] is None


def test_no_key_or_token_value_in_the_body(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("access-tok-123\n")
    s = Settings(
        drafts_dir=tmp_path / "drafts",
        viewer_dist=FIXTURE_DIST,
        pv_mode="hare",
        api_key=KEY,
        access_token_file=token_file,
    )
    with TestClient(create_app(s, startup_hooks=[])) as c:
        c.app.state.http = make_client(s, transport=httpx.MockTransport(Data(_json(SUMMARY))))
        r = c.get("/config.json", headers={"x-canopy-access": "access-tok-123"})
    assert r.status_code == 200
    assert r.json()["binding"] == "va@3"
    assert KEY not in r.text
    assert "access-tok-123" not in r.text


def test_extra_summary_fields_are_not_copied(settings: Settings) -> None:
    body = _config(settings, Data(_json({**SUMMARY, "api_key": KEY})))[0].json()
    assert body["backend"] == {"name": "sim", "label": "Simulator", "notice": None}
    assert KEY not in str(body)


# --- every failure gives both null -------------------------------------------------


def test_missing_shared_client_gives_nulls(settings: Settings) -> None:
    # Without the lifespan there is no app.state.http.
    r = TestClient(create_app(settings, startup_hooks=[])).get("/config.json")
    assert r.status_code == 200
    assert _nulls(r.json())


def test_timeout_is_one_second_and_gives_nulls(settings: Settings) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json=SUMMARY)

    assert static.SUMMARY_TIMEOUT_S == 1.0
    r, elapsed = _config(settings, Data(hang))
    assert r.status_code == 200
    assert _nulls(r.json())
    assert 0.9 <= elapsed < 3.0


def test_transport_error_gives_nulls(settings: Settings) -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    r, _ = _config(settings, Data(refuse))
    assert r.status_code == 200
    assert _nulls(r.json())


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_non_200_gives_nulls(settings: Settings, status: int) -> None:
    r, _ = _config(settings, Data(_json(SUMMARY, status=status)))
    assert r.status_code == 200
    assert _nulls(r.json())


def test_non_json_body_gives_nulls(settings: Settings) -> None:
    async def text(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    assert _nulls(_config(settings, Data(text))[0].json())


@pytest.mark.parametrize(
    "body",
    [
        [SUMMARY],
        "sim",
        None,
        {k: v for k, v in SUMMARY.items() if k != "backend"},
        {**SUMMARY, "backend": None},
        {**SUMMARY, "backend": 3},
        {**SUMMARY, "label": 7},
        {**SUMMARY, "notice": {"x": 1}},
        {**SUMMARY, "binding": ["va@3"]},
        {**SUMMARY, "connected": False},
        {**SUMMARY, "activeMonitors": True},
        {**SUMMARY, "connected": "0"},
        {**SUMMARY, "connected": None},
        {k: v for k, v in SUMMARY.items() if k != "connected"},
    ],
    ids=[
        "list",
        "string",
        "null",
        "no-backend",
        "null-backend",
        "int-backend",
        "int-label",
        "dict-notice",
        "list-binding",
        "bool-connected",
        "bool-active-monitors",
        "string-connected",
        "null-connected",
        "no-connected",
    ],
)
def test_bad_body_gives_nulls(settings: Settings, body) -> None:
    r, _ = _config(settings, Data(_json(body)))
    assert r.status_code == 200
    assert _nulls(r.json())


def test_unreachable_data_gives_nulls(tmp_path: Path) -> None:
    # Port 9 (discard) on loopback: nothing listens, the connect is refused.
    s = Settings(
        drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, pv_mode="hare", base_url="http://127.0.0.1:9"
    )
    r, elapsed = _config(s, None)
    assert r.status_code == 200
    assert _nulls(r.json())
    assert elapsed < 3.0


# --- mode and stub -----------------------------------------------------------------


def test_mock_mode_has_neither_key_and_never_asks_data(tmp_path: Path) -> None:
    s = Settings(drafts_dir=tmp_path / "drafts", viewer_dist=FIXTURE_DIST, pv_mode="mock")
    data = Data(_json(SUMMARY))
    body = _config(s, data)[0].json()
    assert "backend" not in body
    assert "binding" not in body
    assert data.requests == []


def test_config_stays_cross_origin_and_uncached(settings: Settings) -> None:
    r, _ = _config(settings, Data(_json(SUMMARY)))
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.real_data_summary
def test_viewer_config_itself_carries_no_backend(settings: Settings) -> None:
    """The fetch belongs to the route; ``viewer_config`` stays a pure function of settings."""
    assert {"backend", "binding"}.isdisjoint(static.viewer_config(settings))
