"""Converter conversion endpoint (HTTP layer)."""

from pathlib import Path

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.converter.app import app as converter_app
from canopy.converter.config import ConverterConfig
from canopy.contracts.formula import FormulaValidationResult
from canopy.converter.dependencies import (
    get_config,
    get_fox_validate,
    get_conversion_service,
)
from canopy.converter.services.conversion_service import (
    ConversionStats,
    ConversionResult,
    ConvertedFormula,
)

FIXTURES = Path(__file__).parent / "fixtures"
EDL = (FIXTURES / "min.edl").read_bytes()


def _post(client, *, headers=READ_HEADERS, filename="min.edl", content=EDL, data=None):
    return client.post(
        "/converter/v1/conversions",
        headers=headers,
        data=data or {},
        files={"file": (filename, content, "application/octet-stream")},
    )


def test_convert_edl_returns_screen_ir():
    with TestClient(app) as client:
        resp = _post(client)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"screen", "warnings", "unknownWidgets", "formulas", "stats"}
    assert body["screen"]["schemaVersion"] == "1.0"
    assert body["stats"]["widgetCount"] >= 1
    assert body["unknownWidgets"] == []


def test_kind_can_be_given_explicitly_without_suffix():
    with TestClient(app) as client:
        resp = _post(client, filename="noext", data={"kind": "edl"})
    assert resp.status_code == 200


def test_unknown_suffix_is_unsupported():
    with TestClient(app) as client:
        resp = _post(client, filename="foo.txt", content=b"nonsense")
    assert resp.status_code == 415
    assert resp.json()["code"] == "converter_unsupported_input"


def test_requires_api_key():
    with TestClient(app) as client:
        resp = _post(client, headers={})
    assert resp.status_code == 401


def test_formula_validation_warnings_are_surfaced():
    """The route runs hoisted formulas through Fox and folds failures into warnings."""
    fake_result = ConversionResult(
        screen={"schemaVersion": "1.0", "id": "x"},
        warnings=["base warning"],
        unknown_widgets=[],
        formulas=[ConvertedFormula(name="f-1", kind="scalar", expression="bad(", bindings={})],
        stats=ConversionStats(widget_count=0, macro_count=0, rule_count=0),
    )

    class _FakeService:
        def convert(self, _data, *, kind):
            return fake_result

    async def _fake_validate(_spec):
        return FormulaValidationResult(valid=False, errors=["syntax error"])

    converter_app.dependency_overrides[get_conversion_service] = lambda: _FakeService()
    converter_app.dependency_overrides[get_fox_validate] = lambda: _fake_validate
    try:
        with TestClient(app) as client:
            resp = _post(client)
    finally:
        converter_app.dependency_overrides.pop(get_conversion_service, None)
        converter_app.dependency_overrides.pop(get_fox_validate, None)

    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert "base warning" in warnings
    assert any("f-1" in w and "Fox validation" in w for w in warnings)


def test_validate_returns_diagnostics_only():
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/validate",
            headers=READ_HEADERS,
            files={"file": ("min.edl", EDL, "application/octet-stream")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "screen" not in body  # the IR is discarded on a dry run
    assert body["unknownWidgets"] == []
    assert not any("placeholder" in w for w in body["warnings"])


def test_validate_unsupported_input_returns_ok_false():
    """A dry run of an undeterminable kind folds into ok=False, not a 415 envelope."""
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/validate",
            headers=READ_HEADERS,
            files={"file": ("foo.txt", b"nonsense", "application/octet-stream")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["warnings"]  # carries the UnsupportedInput detail


def test_upload_exceeding_limit_is_rejected():
    """An oversized upload returns 413 (the size guard fires before conversion)."""
    converter_app.dependency_overrides[get_config] = lambda: ConverterConfig(max_upload_bytes=16)
    try:
        with TestClient(app) as client:
            resp = _post(client, content=b"x" * 64)
    finally:
        converter_app.dependency_overrides.pop(get_config, None)
    assert resp.status_code == 413
    assert resp.json()["code"] == "converter_upload_too_large"


def test_validate_requires_api_key():
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/validate",
            files={"file": ("min.edl", EDL, "application/octet-stream")},
        )
    assert resp.status_code == 401


def test_convert_ui_input_returns_screen_ir():
    ui = (FIXTURES / "widgets.ui").read_bytes()
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions",
            headers=READ_HEADERS,
            files={"file": ("widgets.ui", ui, "application/octet-stream")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["screen"]["schemaVersion"] == "1.0"
    assert body["stats"]["widgetCount"] >= 1


# --- Batch (folder) conversion --------------------------------------------------

import io  # noqa: E402
import zipfile  # noqa: E402
from uuid import uuid4  # noqa: E402

import fakeredis  # noqa: E402
import fakeredis.aioredis  # noqa: E402

from canopy.jobs import gateway as jobs_gateway  # noqa: E402


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _patch_jobs(monkeypatch):
    """Wire the lazily-imported Jobs gateway to a fakeredis pool + capturing enqueue.

    Returns a *sync* client over the same in-memory FakeServer so tests can seed
    keys without crossing event loops (the async pool the route uses binds to the
    request loop).
    """
    server = fakeredis.FakeServer()
    captured = {}

    async def fake_get_pool():
        return fakeredis.aioredis.FakeRedis(server=server)

    async def fake_enqueue(function, *args, **kwargs):
        captured["function"] = function
        captured["args"] = args
        return uuid4().hex

    monkeypatch.setattr(jobs_gateway, "get_pool", fake_get_pool)
    monkeypatch.setattr(jobs_gateway, "enqueue", fake_enqueue)
    return fakeredis.FakeStrictRedis(server=server), captured


def test_batch_enqueues_and_returns_job(monkeypatch):
    _, captured = _patch_jobs(monkeypatch)
    archive = _zip_bytes({"a.edl": EDL})
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/batch",
            headers=READ_HEADERS,
            files={"file": ("screens.zip", archive, "application/zip")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["statusUrl"].startswith("/jobs/v1/")
    # Enqueued the batch task with the staging token; archive staged under that token.
    assert captured["function"] == "convert_batch"
    assert captured["args"]  # (token,)


def test_batch_rejects_non_archive(monkeypatch):
    _patch_jobs(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/batch",
            headers=READ_HEADERS,
            files={"file": ("a.edl", EDL, "application/octet-stream")},
        )
    assert resp.status_code == 415
    assert resp.json()["code"] == "converter_unsupported_input"


def test_batch_rejects_oversized_archive(monkeypatch):
    _patch_jobs(monkeypatch)
    converter_app.dependency_overrides[get_config] = lambda: ConverterConfig(max_archive_bytes=16)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/converter/v1/conversions/batch",
                headers=READ_HEADERS,
                files={"file": ("screens.zip", _zip_bytes({"a.edl": EDL}), "application/zip")},
            )
    finally:
        converter_app.dependency_overrides.pop(get_config, None)
    assert resp.status_code == 413


def test_batch_requires_api_key():
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions/batch",
            files={"file": ("screens.zip", _zip_bytes({"a.edl": EDL}), "application/zip")},
        )
    assert resp.status_code == 401


def test_get_batch_screen_hit(monkeypatch):
    from canopy.converter.tasks import result_key

    seed, _ = _patch_jobs(monkeypatch)
    job_id, index = "job-xyz", 3
    seed.set(result_key(job_id, index), '{"schemaVersion": "1.0", "id": "s"}')

    with TestClient(app) as client:
        resp = client.get(
            f"/converter/v1/conversions/batch/{job_id}/screens/{index}",
            headers=READ_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["screen"]["schemaVersion"] == "1.0"


def test_get_batch_screen_miss_returns_404(monkeypatch):
    _patch_jobs(monkeypatch)
    with TestClient(app) as client:
        resp = client.get(
            "/converter/v1/conversions/batch/job-xyz/screens/999",
            headers=READ_HEADERS,
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "converter_batch_result_not_found"
