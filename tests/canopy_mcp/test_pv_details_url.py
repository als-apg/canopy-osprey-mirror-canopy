"""``CANOPY_PV_DETAILS_URL``: the page about one PV, handed to the viewer as ``config.json``'s ``pvDetails``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from canopy_mcp.config import Settings, get_settings
from canopy_mcp.static import viewer_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CANOPY_PV_DETAILS_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_means_no_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is not None:
        monkeypatch.setenv("CANOPY_PV_DETAILS_URL", value)
    settings = Settings()
    assert settings.pv_details_url is None
    assert "pvDetails" not in viewer_config(settings)


@pytest.mark.parametrize(
    "url",
    ["https://host/channels/#/channel/{pv}", "http://host/pv?name={pv}", "../channels/#/channel/{pv}", "/pv/{pv}"],
)
def test_a_url_is_read_from_env_and_emitted(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("CANOPY_PV_DETAILS_URL", url)
    settings = get_settings()
    assert settings.pv_details_url == url
    assert viewer_config(settings)["pvDetails"] == {"url": url}


@pytest.mark.parametrize("url", ["https://host/channels", "javascript:alert('{pv}')", "file:///pv/{pv}"])
def test_a_url_the_viewer_would_not_open_stops_the_server(url: str) -> None:
    with pytest.raises(ValidationError, match="CANOPY_PV_DETAILS_URL"):
        Settings(pv_details_url=url)
