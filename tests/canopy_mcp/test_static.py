"""Headers on the draft route: images are sandboxed, screens are left alone."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

FIXTURE_DIST = Path(__file__).parent / "fixtures" / "dist"
CSP = "default-src 'none'; sandbox"


@pytest.fixture
def client(tmp_path: Path):
    root = tmp_path / "drafts"
    (root / "screens" / "bts").mkdir(parents=True)
    (root / "screens" / "bts" / "view.screen.json").write_text(json.dumps({"metadata": {"title": "BTS"}}))
    (root / "screens" / "bts" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (root / "screens" / "bts" / "icon.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    settings = Settings(drafts_dir=root, viewer_dist=FIXTURE_DIST, pv_mode="hare", api_key="k")
    with TestClient(create_app(settings, startup_hooks=[])) as c:
        yield c


@pytest.mark.parametrize("path", ["/drafts/screens/bts/logo.png", "/drafts/screens/bts/icon.svg"])
def test_image_responses_carry_csp_and_nosniff(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["etag"]
    r304 = client.get(path, headers={"if-none-match": r.headers["etag"]})
    assert r304.status_code == 304
    assert r304.headers["content-security-policy"] == CSP
    assert r304.headers["x-content-type-options"] == "nosniff"


def test_screen_responses_carry_no_csp(client: TestClient) -> None:
    r = client.get("/drafts/screens/bts/view.screen.json")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert "x-content-type-options" not in r.headers
