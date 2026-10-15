"""Serving the waveform page: route, config entry and image check.

The page is react-trace's third vite entry, ``waveform.html``, shipped in the
same dist as Trace and the correlation page. canopy_mcp serves it at
``/trace/waveform.html`` exactly like the Trace index (same ``<base href>``,
CSP and no-cache), and advertises it in ``config.json`` only when the file is
installed, so an image built from a react-trace without the page keeps working
and advertises nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import hashlib
import subprocess
from pathlib import Path
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from canopy_mcp.app import create_app
from canopy_mcp.config import Settings

TRACE_CSP = (
    "script-src 'self'; object-src 'none'; base-uri 'self'; frame-src 'none'; "
    "worker-src 'none'; form-action 'none'; frame-ancestors 'self'"
)
PREFIX = "/panel/canopy/u-1.a~b"
WAVE_QUERY = "pv=SR01:BPM:WF:X&pv=SR01:BPM:WF:Y"
CORR_QUERY = "x=SR:DCCT&y=BTS:DIAG:BPM:01:POSITION:X&range=-1h"
TRACE_QUERY = "pv=BTS:DIAG:BPM:01:POSITION:X&range=-1h"
REPO = Path(__file__).resolve().parents[2]
VERIFY = REPO / "scripts" / "verify-image.sh"

PAGE = (
    '<!doctype html><html><head><base href="/"><script type="module" src="./assets/waveform.js"></script>'
    "</head><body>waveform</body></html>"
)


def _bases(body: str) -> list[str]:
    return re.findall(r'<base href="([^"]*)">', body)


def _make_dist(root: Path, *, index: bool = True, waveform: bool = True, correlation: bool = True) -> Path:
    (root / "assets").mkdir(parents=True)
    if index:
        (root / "index.html").write_text(
            '<!doctype html><html><head><script src="./assets/app.js"></script></head><body></body></html>'
        )
    if waveform:
        (root / "waveform.html").write_text(PAGE)
    if correlation:
        (root / "correlation.html").write_text('<script src="./assets/correlation.js"></script>')
    (root / "assets" / "app.js").write_text("console.log('trace')")
    (root / "assets" / "waveform.js").write_text("console.log('wave')")
    return root


def _settings(tmp_path: Path, trace_dist: Path | None, query: str = "") -> Settings:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return Settings(
        drafts_dir=drafts,
        trace_dist=trace_dist,
        trace_default_query=TRACE_QUERY,
        correlation_default_query=CORR_QUERY,
        waveform_default_query=query,
        pv_mode="hare",
        api_key="k",
    )


def _client(tmp_path: Path, trace_dist: Path | None, query: str = "") -> TestClient:
    app = create_app(_settings(tmp_path, trace_dist, query), startup_hooks=[])
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path, _make_dist(tmp_path / "trace"), WAVE_QUERY) as c:
        yield c


# --- settings --------------------------------------------------------------------


def test_waveform_query_defaults_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CANOPY_WAVEFORM_DEFAULT_QUERY", raising=False)
    assert Settings().waveform_default_query == ""


def test_waveform_query_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_WAVEFORM_DEFAULT_QUERY", WAVE_QUERY)
    assert Settings().waveform_default_query == WAVE_QUERY


# --- route -----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_served_with_trace_base_csp_no_cache(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/waveform.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == ["/trace/"]
        assert "./assets/waveform.js" in r.text
    else:
        assert r.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_honours_forwarded_prefix(client: TestClient, method: str) -> None:
    r = client.request(method, "/trace/waveform.html", headers={"x-forwarded-prefix": PREFIX})
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == TRACE_CSP
    assert r.headers["cache-control"] == "no-cache"
    if method == "GET":
        assert _bases(r.text) == [f"{PREFIX}/trace/"]


@pytest.mark.parametrize("prefix", ['/x"><script>', "//evil.example", "http://evil"])
def test_page_rejects_unsafe_prefix(client: TestClient, prefix: str) -> None:
    r = client.get("/trace/waveform.html", headers={"x-forwarded-prefix": prefix})
    assert _bases(r.text) == ["/trace/"]
    assert "<script>" not in r.text


def test_page_assets_served_from_trace_assets(client: TestClient) -> None:
    assert client.get("/trace/assets/waveform.js").status_code == 200


def test_route_does_not_disturb_neighbours(client: TestClient) -> None:
    assert client.get("/trace/").status_code == 200
    assert client.get("/trace/correlation.html").status_code == 200
    assert client.get("/trace/other.html").status_code == 404
    assert client.get("/trace/waveform.html/").status_code == 404
    assert client.get("/trace").status_code == 404


def test_page_404_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", waveform=False), WAVE_QUERY) as c:
        assert c.get("/trace/waveform.html").status_code == 404
        assert c.head("/trace/waveform.html").status_code == 404
        assert c.get("/trace/").status_code == 200


def test_page_404_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, WAVE_QUERY) as c:
        assert c.get("/trace/waveform.html").status_code == 404


def test_page_appearing_after_start_is_served_and_advertised(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path / "trace", waveform=False)
    with _client(tmp_path, dist, WAVE_QUERY) as c:
        assert "waveform" not in c.get("/config.json").json()
        (dist / "waveform.html").write_text(PAGE)
        assert c.get("/trace/waveform.html").status_code == 200
        assert c.get("/config.json").json()["waveform"]["path"] == "trace/waveform.html"


# --- config.json -----------------------------------------------------------------


def test_config_advertises_page_with_query(client: TestClient) -> None:
    config = client.get("/config.json").json()
    assert config["waveform"] == {"path": "trace/waveform.html", "query": WAVE_QUERY}
    assert config["correlation"] == {"path": "trace/correlation.html", "query": CORR_QUERY}
    assert config["trace"] == {"path": "trace/", "query": TRACE_QUERY}


def test_config_query_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _make_dist(tmp_path / "trace")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    monkeypatch.setenv("CANOPY_WAVEFORM_DEFAULT_QUERY", WAVE_QUERY)
    monkeypatch.setenv("CANOPY_TRACE_DIST", str(dist))
    monkeypatch.setenv("CANOPY_DRAFTS_DIR", str(drafts))
    with TestClient(create_app(Settings(), startup_hooks=[])) as c:
        assert c.get("/config.json").json()["waveform"]["query"] == WAVE_QUERY


def test_config_query_empty_by_default(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace")) as c:
        assert c.get("/config.json").json()["waveform"] == {"path": "trace/waveform.html", "query": ""}


def test_config_omits_page_without_file(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", waveform=False), WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert "waveform" not in config
    assert "trace" in config
    assert "correlation" in config


def test_config_waveform_independent_of_correlation(tmp_path: Path) -> None:
    with _client(tmp_path, _make_dist(tmp_path / "trace", correlation=False), WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert config["waveform"] == {"path": "trace/waveform.html", "query": WAVE_QUERY}
    assert "correlation" not in config


def test_config_omits_page_without_trace_dist(tmp_path: Path) -> None:
    with _client(tmp_path, None, WAVE_QUERY) as c:
        config = c.get("/config.json").json()
    assert "waveform" not in config
    assert "trace" not in config


# --- verify-image.sh -------------------------------------------------------------

LOCK_TEXT = "lockfileVersion: '9.0'\n"

# docker stub: `image inspect` answers from $LABELS/<name>; `run ... sh -c SCRIPT`
# runs SCRIPT locally with /app/ mapped to $APPROOT/app/.
DOCKER_STUB = r"""#!/bin/sh
case "$1 $2" in
    "image inspect")
        if [ "$3" = "-f" ]; then
            name=$(printf '%s' "$4" | sed 's/.*Labels "\([^"]*\)".*/\1/')
            cat "$LABELS/$name"
        fi
        exit 0 ;;
esac
if [ "$1" = run ]; then
    while [ "$#" -gt 0 ] && [ "$1" != "-c" ]; do shift; done
    script=$(printf '%s' "$2" | sed "s#/app/#$APPROOT/app/#g")
    exec sh -c "$script"
fi
exit 97
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def image(tmp_path: Path) -> dict:
    """A fake image (labels + /app tree) matching five throwaway checkouts."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    can = tmp_path / "can"
    (can / "scripts").mkdir(parents=True)
    shutil.copy2(VERIFY, can / "scripts" / "verify-image.sh")
    _git(can, "init", "-q", "-b", "main")
    _git(can, "add", "-A")
    _git(can, "commit", "-q", "-m", "init")
    rt = _repo(tmp_path / "rt", {"package.json": "{}\n"})
    trace = _repo(tmp_path / "trace", {"package.json": "{}\n", "pnpm-lock.yaml": LOCK_TEXT})
    sdk = _repo(tmp_path / "sdk", {"package.json": "{}\n"})
    pydm = _repo(tmp_path / "pydm", {"pyproject.toml": "[project]\nname='PyDMConverter'\n"})

    labels = tmp_path / "labels"
    labels.mkdir()
    for name, value in {
        "org.canopy.sha": _git(can, "rev-parse", "HEAD"),
        "org.canopy.runtime.sha": _git(rt, "rev-parse", "HEAD"),
        "org.canopy.sdk.sha": _git(sdk, "rev-parse", "HEAD"),
        "org.canopy.pydm.sha": _git(pydm, "rev-parse", "HEAD"),
        "org.canopy.trace.sha": _git(trace, "rev-parse", "HEAD"),
        "org.canopy.trace.lockfile-sha256": hashlib.sha256(LOCK_TEXT.encode()).hexdigest(),
    }.items():
        (labels / name).write_text(value + "\n")

    root = tmp_path / "root"
    (root / "app" / "viewer").mkdir(parents=True)
    (root / "app" / "viewer" / "index.html").write_text("<html></html>")
    (root / "app" / "trace").mkdir(parents=True)
    (root / "app" / "trace" / "index.html").write_text('<script src="./assets/app.js"></script>')

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if k not in {"TRACE", "TRACE_REF", "RT", "SDK", "PYDM"}}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        TRACE=str(trace),
        TRACE_REF="main",
        RT=str(rt),
        SDK=str(sdk),
        PYDM=str(pydm),
        LABELS=str(labels),
        APPROOT=str(root),
    )
    return {"can": can, "env": env, "trace_dir": root / "app" / "trace"}


def _verify(image: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(image["can"] / "scripts" / "verify-image.sh"), "canopy:test"],
        env=image["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_verify_passes_without_waveform_page(image: dict) -> None:
    r = _verify(image)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "waveform" not in r.stdout + r.stderr


def test_verify_passes_with_waveform_page(image: dict) -> None:
    (image["trace_dir"] / "waveform.html").write_text(PAGE)
    r = _verify(image)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checked /app/trace/waveform.html" in r.stderr.replace(image["env"]["APPROOT"], "")


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ('<script src="/assets/waveform.js"></script>', "no ./assets/ reference"),
        ('<script src="./assets/w.js"></script><link href="/assets/x.css">', "an absolute /assets/ URL"),
        ('<script src="./assets/w.js"></script><link href="/favicon.svg">', "an absolute /favicon.svg URL"),
    ],
)
def test_verify_fails_on_absolute_urls_in_waveform_page(image: dict, body: str, complaint: str) -> None:
    (image["trace_dir"] / "waveform.html").write_text(body)
    r = _verify(image)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"waveform.html has {complaint}" in r.stderr
