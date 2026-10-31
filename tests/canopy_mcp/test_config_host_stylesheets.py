"""``CANOPY_HOST_STYLESHEETS``: the host's stylesheet paths, checked at startup.

The setting is a whitespace-separated list of relative ``.css`` paths the
served pages link under the proxy prefix. Each entry ends up inside an
``href``, so an entry that could leave the prefix, name another host or break
out of the attribute stops the server before anything is served.
"""

import pytest
from pydantic import ValidationError

from canopy_mcp.config import Settings, get_settings

HOST_PATHS = [
    "design-system/css/tokens.css",
    "static/fonts/fonts.css",
    "design-system/css/canopy-host.css",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("CANOPY_HOST_STYLESHEETS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unset_is_an_empty_list():
    settings = Settings()
    assert settings.host_stylesheets == ""
    assert settings.host_stylesheet_paths == []


@pytest.mark.parametrize("value", ["", "   ", " \n\t "])
def test_an_empty_setting_is_an_empty_list(monkeypatch, value):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", value)
    assert Settings().host_stylesheet_paths == []


def test_three_host_paths_are_accepted_in_order(monkeypatch):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", " ".join(HOST_PATHS))
    assert Settings().host_stylesheet_paths == HOST_PATHS


def test_any_whitespace_separates_entries(monkeypatch):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", "  a.css\n\tb/c.css  d_e-f.min.css ")
    assert Settings().host_stylesheet_paths == ["a.css", "b/c.css", "d_e-f.min.css"]


def test_the_cached_settings_read_the_variable(monkeypatch):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", "a.css")
    assert get_settings().host_stylesheet_paths == ["a.css"]


@pytest.mark.parametrize(
    "entry",
    [
        "//h/x.css",  # protocol-relative: another host
        "../x.css",  # leaves the prefix
        'a"b.css',  # closes the href attribute
        "/abs.css",  # absolute: ignores the prefix
        "x.js",  # not a stylesheet
        "a/../x.css",  # dot segment further in
        "a//x.css",  # empty segment
        "a/.hidden.css",  # segment starting with a dot
        ".css",  # no name
        "http://h/x.css",  # a scheme
        "x.css?y",  # a query
        "x.css/",  # trailing slash
    ],
)
def test_an_entry_outside_the_grammar_is_refused_by_name(monkeypatch, entry):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", entry)
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert repr(entry) in str(caught.value)
    assert "CANOPY_HOST_STYLESHEETS" in str(caught.value)


def test_one_bad_entry_among_good_ones_is_the_one_named(monkeypatch):
    monkeypatch.setenv("CANOPY_HOST_STYLESHEETS", f"{HOST_PATHS[0]} ../x.css {HOST_PATHS[1]}")
    with pytest.raises(ValidationError) as caught:
        Settings()
    message = str(caught.value)
    assert "'../x.css'" in message
    assert f"{HOST_PATHS[0]!r}" not in message


def test_a_bad_value_passed_by_keyword_is_refused_too():
    with pytest.raises(ValidationError):
        Settings(host_stylesheets="/abs.css")
