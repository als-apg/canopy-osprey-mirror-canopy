"""``GET /channels``: the build-time channel catalog Trace autocompletes from."""

import json
import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

CATALOG = ["SIM:BPM1:X", "SIM:BPM1:Y", "SIM:MAG:QF1:CURRENT"]
PREFIX = "/panel/canopy/u-1"


def _client(tmp_path: Path, channels_file: Path | None) -> TestClient:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    settings = Settings(drafts_dir=drafts, channels_file=channels_file, pv_mode="hare", api_key="k")
    return TestClient(create_app(settings, startup_hooks=[]), follow_redirects=False)


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps(CATALOG))
    return path


def _etag(path: Path) -> str:
    return f'"{hashlib.sha256(path.read_bytes()).hexdigest()}"'


def test_settings_read_the_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CANOPY_CHANNELS_FILE", raising=False)
    assert Settings().channels_file is None
    monkeypatch.setenv("CANOPY_CHANNELS_FILE", str(tmp_path / "c.json"))
    assert Settings().channels_file == tmp_path / "c.json"


def test_serves_the_catalog_with_a_strong_etag(tmp_path: Path, catalog: Path) -> None:
    with _client(tmp_path, catalog) as client:
        response = client.get("/channels")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"] == _etag(catalog)
    assert response.json() == CATALOG


def test_a_matching_etag_answers_304(tmp_path: Path, catalog: Path) -> None:
    with _client(tmp_path, catalog) as client:
        response = client.get("/channels", headers={"If-None-Match": _etag(catalog)})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == _etag(catalog)
    assert response.headers["cache-control"] == "no-cache"


def test_a_stale_etag_gets_the_new_bytes(tmp_path: Path, catalog: Path) -> None:
    with _client(tmp_path, catalog) as client:
        old = client.get("/channels").headers["etag"]
        catalog.write_text(json.dumps(["SIM:NEW:CHANNEL"]))
        response = client.get("/channels", headers={"If-None-Match": old})
    assert response.status_code == 200
    assert response.json() == ["SIM:NEW:CHANNEL"]
    assert response.headers["etag"] != old


def test_unset_means_no_catalog(tmp_path: Path) -> None:
    with _client(tmp_path, None) as client:
        response = client.get("/channels")
    assert response.status_code == 404


def test_a_missing_file_means_no_catalog(tmp_path: Path) -> None:
    with _client(tmp_path, tmp_path / "absent.json") as client:
        response = client.get("/channels")
    assert response.status_code == 404


def test_a_directory_is_not_a_catalog(tmp_path: Path) -> None:
    with _client(tmp_path, tmp_path) as client:
        response = client.get("/channels")
    assert response.status_code == 404


def test_reachable_through_the_proxy(tmp_path: Path, catalog: Path) -> None:
    """A browser session (prefixed, via the OSPREY proxy) reaches the route; the MCP guard does not apply."""
    with _client(tmp_path, catalog) as client:
        response = client.get("/channels", headers={"X-Forwarded-Prefix": PREFIX})
    assert response.status_code == 200
    assert response.json() == CATALOG


def test_writes_are_refused(tmp_path: Path, catalog: Path) -> None:
    with _client(tmp_path, catalog) as client:
        response = client.post("/channels", json=[])
    assert response.status_code == 405
