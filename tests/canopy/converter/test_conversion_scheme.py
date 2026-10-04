"""The converter's color scheme: config precedence, injection into convert_bytes, and the DTO ``scheme``."""

import io
import sys
import json
import zipfile
from pathlib import Path

import pytest
import fakeredis.aioredis
from fastapi.testclient import TestClient

import canopy.converter.tasks as tasks_module
from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.converter.app import app as converter_app
from canopy.core.color_scheme import DEFAULT_ID, get_color_scheme, clear_color_scheme_cache
from canopy.converter.config import ConverterConfig
from canopy.converter.dependencies import get_conversion_service
from canopy.converter.services import conversion_service as service_module
from canopy.converter.services.conversion_service import ConversionService

FIXTURES = Path(__file__).parent / "fixtures"
EDL = (FIXTURES / "min.edl").read_bytes()

SCHEME_ENV = ("CANOPY_CONVERTER__SCHEME_PATH", "CANOPY_THEME_FILE", "CANOPY_CONVERTER__COLOR_DEF_PATH")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in SCHEME_ENV:
        monkeypatch.delenv(name, raising=False)
    clear_color_scheme_cache()
    tasks_module._service = None
    yield
    clear_color_scheme_cache()
    tasks_module._service = None


@pytest.fixture
def spy(monkeypatch):
    """Record the kwargs each convert_bytes call receives, then run the real converter."""
    calls: list[dict] = []
    real = service_module.convert_bytes

    def _spy(data, **kwargs):
        calls.append(kwargs)
        return real(data, **kwargs)

    monkeypatch.setattr(service_module, "convert_bytes", _spy)
    return calls


@pytest.fixture
def good_scheme(tmp_path) -> Path:
    path = tmp_path / "facility.json"
    path.write_text(json.dumps({"id": "facility", "extends": "canopy-default", "roles": {"OK": "#00ff00"}}))
    return path


@pytest.fixture
def broken_scheme(tmp_path) -> Path:
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json")
    return path


def _scheme_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if w.startswith("Color scheme")]


# --- config ---------------------------------------------------------------------------------


def test_scheme_path_defaults_to_none():
    config = ConverterConfig()
    assert config.scheme_path is None
    assert config.color_def_path is None


def test_scheme_path_falls_back_to_theme_file(monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", "/srv/theme.json")
    assert ConverterConfig().scheme_path == Path("/srv/theme.json")


def test_converter_scheme_path_wins_over_theme_file(monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", "/srv/theme.json")
    monkeypatch.setenv("CANOPY_CONVERTER__SCHEME_PATH", "/srv/converter.json")
    assert ConverterConfig().scheme_path == Path("/srv/converter.json")


def test_blank_scheme_env_is_unset(monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", "  ")
    monkeypatch.setenv("CANOPY_CONVERTER__COLOR_DEF_PATH", "")
    config = ConverterConfig()
    assert config.scheme_path is None
    assert config.color_def_path is None


def test_color_def_path_from_env(monkeypatch):
    monkeypatch.setenv("CANOPY_CONVERTER__COLOR_DEF_PATH", "/etc/phoebus/color.def")
    assert ConverterConfig().color_def_path == Path("/etc/phoebus/color.def")


def test_fields_settable_by_name():
    config = ConverterConfig(scheme_path="/a.json", color_def_path="/b.def")
    assert config.scheme_path == Path("/a.json")
    assert config.color_def_path == Path("/b.def")


# --- service --------------------------------------------------------------------------------


def test_scheme_free_service_passes_no_scheme_kwargs(spy):
    service = ConversionService()
    result = service.convert(EDL, kind="edl")
    assert "scheme" not in spy[0]
    assert "color_def_path" not in spy[0]
    assert service.scheme_meta is None
    assert result.scheme is None
    assert _scheme_warnings(result.warnings) == []


def test_injected_scheme_result_is_passed_through(spy, good_scheme):
    loaded = get_color_scheme(good_scheme)
    service = ConversionService(scheme=loaded)
    result = service.convert(EDL, kind="edl")
    assert spy[0]["scheme"] is loaded.result
    assert "color_def_path" not in spy[0]
    assert service.scheme_meta == loaded.meta
    assert result.scheme == loaded.meta
    assert result.scheme["id"] == "facility"
    assert _scheme_warnings(result.warnings) == []


def test_color_def_path_passed_only_when_set(spy, tmp_path):
    color_def = tmp_path / "color.def"
    color_def.write_text("")
    ConversionService(color_def_path=color_def).convert(EDL, kind="edl")
    assert spy[0]["color_def_path"] == color_def
    ConversionService(color_def_path=None).convert(EDL, kind="edl")
    assert "color_def_path" not in spy[1]


def test_load_error_loads_once_and_warns_once_per_conversion(monkeypatch, broken_scheme):
    import pydmconverter.scheme as pydm_scheme

    loads = []
    real = pydm_scheme.load_scheme

    def _counting(*args, **kwargs):
        loads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pydm_scheme, "load_scheme", _counting)
    service = ConversionService(scheme=get_color_scheme(broken_scheme))
    # A second service on the same path (e.g. the batch worker) reuses the cached load.
    other = ConversionService(scheme=get_color_scheme(broken_scheme))
    results = [service.convert(EDL, kind="edl"), service.convert(EDL, kind="edl"), other.convert(EDL, kind="edl")]

    assert len(loads) == 1
    for result in results:
        assert len(_scheme_warnings(result.warnings)) == 1
        assert result.scheme["id"] is None
        assert result.scheme["served"] == DEFAULT_ID
        assert result.scheme["errors"]


def test_without_pydm_scheme_converts_scheme_free_with_degraded_warning(monkeypatch, spy):
    monkeypatch.setitem(sys.modules, "pydmconverter.scheme", None)  # import now raises ImportError
    loaded = get_color_scheme(None)
    assert loaded.result is None and loaded.flat_json is None
    result = ConversionService(scheme=loaded).convert(EDL, kind="edl")
    assert "scheme" not in spy[0]
    warnings = _scheme_warnings(result.warnings)
    assert len(warnings) == 1
    assert "unavailable" in warnings[0]
    assert result.scheme["served"] == DEFAULT_ID


# --- HTTP -----------------------------------------------------------------------------------


def _with_service(service: ConversionService):
    converter_app.dependency_overrides[get_conversion_service] = lambda: service


def _post(client, path: str, *, filename: str = "min.edl", content: bytes = EDL):
    return client.post(
        f"/converter/v1{path}",
        headers=READ_HEADERS,
        files={"file": (filename, content, "application/octet-stream")},
    )


@pytest.fixture
def broken_service(broken_scheme):
    _with_service(ConversionService(scheme=get_color_scheme(broken_scheme)))
    yield
    converter_app.dependency_overrides.pop(get_conversion_service, None)


def test_convert_response_carries_scheme_without_flat_hash(broken_service):
    with TestClient(app) as client:
        resp = _post(client, "/conversions")
    assert resp.status_code == 200
    body = resp.json()
    scheme = body["scheme"]
    assert set(scheme) == {"id", "sha256", "served", "errors"}
    assert scheme["id"] is None
    assert scheme["served"] == DEFAULT_ID
    assert scheme["errors"] and set(scheme["errors"][0]) == {"path", "message"}
    assert len(_scheme_warnings(body["warnings"])) == 1


def test_validate_ok_response_carries_scheme(broken_service):
    with TestClient(app) as client:
        resp = _post(client, "/conversions/validate")
    body = resp.json()
    assert body["ok"] is True
    assert body["scheme"]["id"] is None
    assert len(_scheme_warnings(body["warnings"])) == 1


def test_validate_error_response_carries_scheme(broken_service):
    with TestClient(app) as client:
        resp = _post(client, "/conversions/validate", filename="broken.ui", content=b"<not-a-ui/>")
    body = resp.json()
    assert body["ok"] is False
    assert body["scheme"]["id"] is None
    assert body["scheme"]["served"] == DEFAULT_ID


def test_lifespan_injects_configured_scheme(monkeypatch, good_scheme, spy):
    monkeypatch.setenv("CANOPY_CONVERTER__SCHEME_PATH", str(good_scheme))
    with TestClient(app) as client:
        resp = _post(client, "/conversions")
    assert resp.status_code == 200
    assert resp.json()["scheme"]["id"] == "facility"
    assert "scheme" in spy[0]


def test_lifespan_default_scheme_is_canopy_default():
    with TestClient(app) as client:
        resp = _post(client, "/conversions")
    scheme = resp.json()["scheme"]
    assert scheme["id"] == DEFAULT_ID
    assert scheme["served"] == DEFAULT_ID
    assert scheme["errors"] == []
    assert scheme["sha256"]


# --- batch ----------------------------------------------------------------------------------


async def test_batch_service_injects_scheme_and_warns_after_load_error(monkeypatch, broken_scheme, spy):
    monkeypatch.setenv("CANOPY_CONVERTER__SCHEME_PATH", str(broken_scheme))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.edl", EDL)
        zf.writestr("b.edl", EDL)
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(tasks_module.input_key("tok"), buf.getvalue())

    manifest = await tasks_module.convert_batch({"redis": redis, "job_id": "job1"}, "tok")

    assert manifest["succeeded"] == 2
    assert tasks_module._get_service().scheme_meta["id"] is None
    assert all("scheme" in call for call in spy)
    for row in manifest["rows"]:
        assert len(_scheme_warnings(row["warnings"])) == 1
