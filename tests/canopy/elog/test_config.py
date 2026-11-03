"""`ElogConfig` parses the documented ``CANOPY_ELOG__*`` env vars."""

import os

import pytest
from pydantic import SecretStr

from canopy.elog.config import ElogConfig


@pytest.fixture(autouse=True)
def _clean_elog_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CANOPY_ELOG__"):
            monkeypatch.delenv(name)


def test_defaults_leave_the_module_disabled():
    config = ElogConfig()

    assert config.provider == ""
    assert config.apps == {}
    assert config.elog_plus.base_url == ""
    assert config.elog_plus.auth_header == "x-vouch-idp-accesstoken"
    assert config.proxy_url is None
    assert config.timeout_s == 15.0
    assert config.max_attachment_bytes == 20 * 1024 * 1024


def test_per_app_token_lands_in_its_app(monkeypatch):
    monkeypatch.setenv("CANOPY_ELOG__APPS__SQUIRREL__TOKEN", "squirrel-secret")
    monkeypatch.setenv("CANOPY_ELOG__APPS__SQUIRREL__DEFAULT_LOGBOOK_IDS", '["lb-1", "lb-2"]')
    monkeypatch.setenv("CANOPY_ELOG__APPS__TRACE__TOKEN", "trace-secret")

    config = ElogConfig()

    assert set(config.apps) == {"squirrel", "trace"}
    squirrel = config.apps["squirrel"]
    assert isinstance(squirrel.token, SecretStr)
    assert squirrel.token.get_secret_value() == "squirrel-secret"
    assert squirrel.default_logbook_ids == ["lb-1", "lb-2"]
    assert config.apps["trace"].token.get_secret_value() == "trace-secret"
    assert config.apps["trace"].default_logbook_ids == []


def test_whitespace_around_a_token_is_stripped(monkeypatch):
    # A secret file ending in a newline; the header value would otherwise be illegal.
    monkeypatch.setenv("CANOPY_ELOG__APPS__SQUIRREL__TOKEN", "squirrel-secret\n")
    monkeypatch.setenv("CANOPY_ELOG__APPS__TRACE__TOKEN", " \n")

    config = ElogConfig()

    assert config.apps["squirrel"].token.get_secret_value() == "squirrel-secret"
    assert config.apps["trace"].token.get_secret_value() == ""


def test_tokens_never_appear_in_repr(monkeypatch):
    monkeypatch.setenv("CANOPY_ELOG__APPS__SQUIRREL__TOKEN", "squirrel-secret")

    assert "squirrel-secret" not in repr(ElogConfig())


def test_provider_and_nested_elog_plus_settings(monkeypatch):
    monkeypatch.setenv("CANOPY_ELOG__PROVIDER", " elog_plus ")
    monkeypatch.setenv("CANOPY_ELOG__ELOG_PLUS__BASE_URL", "https://elog.example.org/api/elog-apptoken")
    monkeypatch.setenv("CANOPY_ELOG__ELOG_PLUS__AUTH_HEADER", "x-app-token")
    monkeypatch.setenv("CANOPY_ELOG__PROXY_URL", "http://proxy.example.test:3128")
    monkeypatch.setenv("CANOPY_ELOG__TIMEOUT_S", "5")
    monkeypatch.setenv("CANOPY_ELOG__MAX_ATTACHMENT_BYTES", "1024")

    config = ElogConfig()

    assert config.provider == "elog_plus"
    assert config.elog_plus.base_url == "https://elog.example.org/api/elog-apptoken"
    assert config.elog_plus.auth_header == "x-app-token"
    assert config.proxy_url == "http://proxy.example.test:3128"
    assert config.timeout_s == 5.0
    assert config.max_attachment_bytes == 1024


def test_empty_proxy_url_means_no_proxy(monkeypatch):
    monkeypatch.setenv("CANOPY_ELOG__PROXY_URL", "")

    assert ElogConfig().proxy_url is None
