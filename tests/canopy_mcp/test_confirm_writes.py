"""The two write flags: ``CANOPY_CONFIRM_WRITES`` (config.json) and the relay-aware screen check."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.access import ACCESS_HEADER
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.static import viewer_config
from canopy_mcp.catalog import build_catalog
from canopy_mcp.check.lint import RACCOON_VALIDATE_PATH
from canopy_mcp.tools.check import run_check
from canopy_mcp.tools.convert import QUEUE_CODES

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
BASE = "http://canopy.test"
RACCOON = BASE + RACCOON_VALIDATE_PATH
ACCESS = "access-credential"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CANOPY_CONFIRM_WRITES",
        "CANOPY_PUT_RELAY",
        "CANOPY_WRITE_API_KEY_FILE",
        "CANOPY_ACCESS_TOKEN_FILE",
        "CANOPY_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _secret(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value + "\n")
    return path


def _relay_settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        base_url=BASE,
        drafts_dir=tmp_path / "drafts",
        put_relay=True,
        write_api_key_file=_secret(tmp_path, "front.write", "write-key"),
        access_token_file=_secret(tmp_path, "access", ACCESS),
        **extra,
    )


# --- CANOPY_CONFIRM_WRITES ----------------------------------------------------------


def test_confirm_writes_defaults_off() -> None:
    assert Settings().confirm_writes is False


@pytest.mark.parametrize(("raw", "expected"), [("0", False), ("1", True), ("", False)])
def test_confirm_writes_read_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("CANOPY_CONFIRM_WRITES", raw)
    assert Settings().confirm_writes is expected


def test_confirm_writes_garbage_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_CONFIRM_WRITES", "sometimes")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("mode", ["hare", "mock"])
def test_viewer_config_always_carries_confirm_writes(tmp_path: Path, flag: bool, mode: str) -> None:
    s = Settings(drafts_dir=tmp_path, viewer_dist=FIXTURE_DIST, pv_mode=mode, confirm_writes=flag)
    assert viewer_config(s)["confirmWrites"] is flag


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("mode", ["hare", "mock"])
def test_config_json_route_carries_confirm_writes(tmp_path: Path, flag: bool, mode: str) -> None:
    s = Settings(drafts_dir=tmp_path, viewer_dist=FIXTURE_DIST, pv_mode=mode, confirm_writes=flag)
    with TestClient(create_app(s, startup_hooks=[])) as c:
        body = c.get("/config.json").json()
    assert body["confirmWrites"] is flag


def test_confirm_writes_is_independent_of_the_relay(tmp_path: Path) -> None:
    s = _relay_settings(tmp_path, viewer_dist=FIXTURE_DIST)
    assert s.put_relay is True and s.confirm_writes is False
    with TestClient(create_app(s, startup_hooks=[]), headers={ACCESS_HEADER: ACCESS}) as c:
        assert c.get("/config.json").json()["confirmWrites"] is False


# --- the check tool follows CANOPY_PUT_RELAY ------------------------------------------


def _doc(*children: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "kind": "screen",
        "id": "t",
        "metadata": {"title": "t", "source": {"type": "native"}, "size": {"width": 10, "height": 10}},
        "macros": [],
        "root": {
            "id": "root",
            "type": "group",
            "props": {},
            "geometry": {"x": 0, "y": 0, "width": 10, "height": 10},
            "children": list(children),
        },
    }


def _node(nid: str, ntype: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "props": props, "geometry": {"x": 0, "y": 0, "width": 10, "height": 10}}


def _catalog():
    return build_catalog({"group": {"consumes": [], "verbatim": False}})


def _screen() -> dict[str, Any]:
    return _doc(
        _node("machine", "pv-button", {"pv": "SR:X"}),
        _node("local", "pv-toggle", {"pv": "loc://flag"}),
        _node("act", "action-button", {"actions": [{"type": "write_pv", "pvName": "SR:Y", "value": 1}]}),
    )


def _write_codes(result: Any) -> list[tuple[str | None, str]]:
    return [(f.node_id, f.code) for f in result.semantic if f.code in ("write_not_relayed", "writes_to_machine")]


@pytest.mark.parametrize(
    ("relay", "expected"),
    [
        (False, [("machine", "write_not_relayed"), ("local", "write_not_relayed"), ("act", "write_not_relayed")]),
        (True, [("machine", "writes_to_machine"), ("local", "write_not_relayed"), ("act", "writes_to_machine")]),
    ],
)
async def test_run_check_passes_the_relay_setting(
    httpx_mock: HTTPXMock, tmp_path: Path, relay: bool, expected: list[tuple[str, str]]
) -> None:
    httpx_mock.add_response(method="POST", url=RACCOON, json={"known": ["SR:X", "SR:Y"], "unknown": []})
    settings = _relay_settings(tmp_path) if relay else Settings(base_url=BASE, drafts_dir=tmp_path / "drafts")
    (tmp_path / "drafts").mkdir(exist_ok=True)
    client = make_client(settings)
    result = await run_check(_screen(), settings=settings, catalog=_catalog(), client=client)
    assert _write_codes(result) == expected


def test_convert_legacy_queue_codes_are_unchanged() -> None:
    assert QUEUE_CODES == ("warning", "unknown_widget", "unresolved_ref")
    assert "writes_to_machine" not in QUEUE_CODES
