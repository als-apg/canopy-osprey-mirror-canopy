"""get_color_scheme: one flatten per path, canopy-default on any error, degraded on a missing flattener."""

import sys
import json
import hashlib
import logging

import pytest

from canopy.core import color_scheme
from canopy.core.color_scheme import DEFAULT_SCHEME_PATH, get_color_scheme


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("CANOPY_THEME_FILE", raising=False)
    color_scheme.clear_color_scheme_cache()
    yield
    color_scheme.clear_color_scheme_cache()


def _errors(caplog):
    return [r for r in caplog.records if r.name == color_scheme.__name__ and r.levelno == logging.ERROR]


def _packaged_sha256() -> str:
    return hashlib.sha256(DEFAULT_SCHEME_PATH.read_bytes()).hexdigest()


def test_unset_env_serves_canopy_default(caplog):
    scheme = get_color_scheme()
    assert scheme.meta["id"] == "canopy-default"
    assert scheme.meta["served"] == "canopy-default"
    assert scheme.meta["sha256"] == _packaged_sha256()
    assert scheme.meta["errors"] == []
    assert scheme.degraded == []
    assert scheme.result is not None
    assert scheme.flat_json["meta"] == scheme.meta
    assert "Text" in scheme.flat_json["roles"]
    assert scheme.imports["phoebus"]
    assert _errors(caplog) == []


def test_empty_env_counts_as_unset(monkeypatch):
    monkeypatch.setenv("CANOPY_THEME_FILE", "")
    assert get_color_scheme().meta["id"] == "canopy-default"


def test_valid_file_gives_facility_id(tmp_path, caplog):
    path = tmp_path / "facility.json"
    path.write_text(json.dumps({"id": "facility-x", "extends": "canopy-default", "roles": {"Beam": "#123456"}}))
    scheme = get_color_scheme(path)
    assert scheme.meta["id"] == "facility-x"
    assert scheme.meta["served"] == "facility-x"
    assert scheme.meta["errors"] == []
    assert scheme.degraded == []
    assert "Beam" in scheme.flat_json["roles"]
    assert _errors(caplog) == []


def test_env_var_names_the_file(tmp_path, monkeypatch):
    path = tmp_path / "facility.json"
    path.write_text(json.dumps({"id": "from-env", "extends": "canopy-default", "roles": {}}))
    monkeypatch.setenv("CANOPY_THEME_FILE", str(path))
    assert get_color_scheme().meta["id"] == "from-env"


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        json.dumps({"id": "broken", "extends": "canopy-default", "roles": {"Beam": "@NoSuchRole"}}),
    ],
    ids=["unparseable", "broken-but-parseable"],
)
def test_bad_file_logs_once_and_serves_default(tmp_path, caplog, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with caplog.at_level(logging.ERROR, logger=color_scheme.__name__):
        scheme = get_color_scheme(path)
    assert len(_errors(caplog)) == 1
    assert scheme.degraded == ["color-scheme"]
    assert scheme.meta["served"] == "canopy-default"
    assert scheme.meta["errors"]
    assert scheme.flat_json is not None
    assert "Beam" not in scheme.flat_json["roles"]


def test_missing_file_is_degraded(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger=color_scheme.__name__):
        scheme = get_color_scheme(tmp_path / "absent.json")
    assert len(_errors(caplog)) == 1
    assert scheme.degraded == ["color-scheme"]
    assert scheme.meta["served"] == "canopy-default"


def test_second_call_same_path_does_not_reflatten(tmp_path, caplog, monkeypatch):
    from pydmconverter import scheme as pydm_scheme

    calls = []
    real = pydm_scheme.load_scheme

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pydm_scheme, "load_scheme", spy)
    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    with caplog.at_level(logging.ERROR, logger=color_scheme.__name__):
        first = get_color_scheme(path)
        # A relative spelling of the same file hits the same cache entry.
        monkeypatch.chdir(tmp_path)
        second = get_color_scheme("bad.json")
    assert second is first
    assert len(calls) == 1
    assert len(_errors(caplog)) == 1


def test_default_is_cached_too(monkeypatch):
    first = get_color_scheme()
    assert get_color_scheme(None) is first


def test_import_error_degrades_without_flat_json(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "pydmconverter.scheme", None)
    with caplog.at_level(logging.ERROR, logger=color_scheme.__name__):
        scheme = get_color_scheme()
        again = get_color_scheme()
    assert scheme.result is None
    assert scheme.flat_json is None
    assert scheme.degraded == ["color-scheme"]
    assert scheme.meta["errors"]
    assert scheme.meta["served"] == "canopy-default"
    assert again is scheme
    assert len(_errors(caplog)) == 1


def test_canonical_default_in_sync_with_pydm():
    from pydmconverter import scheme as pydm_scheme

    assert DEFAULT_SCHEME_PATH.read_bytes() == pydm_scheme.DEFAULT_SCHEME_PATH.read_bytes()
    result = pydm_scheme.load_scheme(None, base_path=DEFAULT_SCHEME_PATH)
    assert result.meta["errors"] == []
