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
EDL = (FIXTURES / "p0_min.edl").read_bytes()


def _post(client, *, headers=READ_HEADERS, filename="p0_min.edl", content=EDL, data=None):
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
    assert body["unknownWidgets"][0]["originalClass"] == "activeRectangleClass"


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
            files={"file": ("p0_min.edl", EDL, "application/octet-stream")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "screen" not in body  # the IR is discarded on a dry run
    assert body["unknownWidgets"][0]["originalClass"] == "activeRectangleClass"
    assert any("placeholder" in w for w in body["warnings"])


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
            files={"file": ("p0_min.edl", EDL, "application/octet-stream")},
        )
    assert resp.status_code == 401


def test_convert_ui_input_returns_screen_ir():
    ui = (FIXTURES / "p1_widgets.ui").read_bytes()
    with TestClient(app) as client:
        resp = client.post(
            "/converter/v1/conversions",
            headers=READ_HEADERS,
            files={"file": ("p1_widgets.ui", ui, "application/octet-stream")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["screen"]["schemaVersion"] == "1.0"
    assert body["stats"]["widgetCount"] >= 1
