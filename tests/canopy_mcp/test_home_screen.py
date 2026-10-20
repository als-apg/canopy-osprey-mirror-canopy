"""``CANOPY_HOME_SCREEN``: the library screen the viewer opens on, handed over as ``config.json``'s ``home``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from canopy_mcp.config import Settings, get_settings
from canopy_mcp.static import viewer_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CANOPY_HOME_SCREEN", raising=False)
    monkeypatch.delenv("CANOPY_WORKSPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unset_means_no_home_screen_and_no_home_key() -> None:
    settings = Settings()
    assert settings.home_screen is None
    assert "home" not in viewer_config(settings)


def test_empty_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_HOME_SCREEN", "")
    settings = Settings()
    assert settings.home_screen is None
    assert "home" not in viewer_config(settings)


@pytest.mark.parametrize("screen", ["sr/overview", "overview", "a/b/c-d_e.f"])
def test_a_set_id_is_read_from_env_and_emitted(monkeypatch: pytest.MonkeyPatch, screen: str) -> None:
    monkeypatch.setenv("CANOPY_HOME_SCREEN", screen)
    settings = get_settings()
    assert settings.home_screen == screen
    assert viewer_config(settings)["home"] == {"screen": screen}


def test_the_home_key_leaves_every_other_key_unchanged() -> None:
    without = viewer_config(Settings())
    with_home = viewer_config(Settings(home_screen="sr/overview"))
    assert with_home.pop("home") == {"screen": "sr/overview"}
    assert with_home == without


@pytest.mark.parametrize(
    "screen",
    ["/sr/overview", "../overview", "sr/../overview", "sr/..", "..", "sr//overview", "sr/overview/", "sr\\overview"],
)
def test_a_value_outside_the_library_stops_start_up(monkeypatch: pytest.MonkeyPatch, screen: str) -> None:
    monkeypatch.setenv("CANOPY_HOME_SCREEN", screen)
    with pytest.raises(ValidationError) as caught:
        get_settings()
    message = str(caught.value)
    assert "CANOPY_HOME_SCREEN" in message
    assert repr(screen) in message


def test_the_keyword_form_is_refused_too() -> None:
    with pytest.raises(ValidationError, match="CANOPY_HOME_SCREEN"):
        Settings(home_screen="../escape")
