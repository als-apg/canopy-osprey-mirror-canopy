"""RaccoonConfig env-var loading: nested directory + cache blocks."""

import pytest

from canopy.raccoon.config import RaccoonConfig


def test_default_config_has_no_directory() -> None:
    cfg = RaccoonConfig()
    assert cfg.directory is None


def test_directory_loaded_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANOPY_RACCOON__DIRECTORY__TYPE", "meme")
    monkeypatch.setenv("CANOPY_RACCOON__DIRECTORY__TIMEOUT_S", "7")
    monkeypatch.setenv("CANOPY_RACCOON__DIRECTORY__CACHE__EXISTS_TTL_S", "900")
    monkeypatch.setenv("CANOPY_RACCOON__DIRECTORY__CACHE__MAX_EXISTS_ENTRIES", "5000")

    cfg = RaccoonConfig()

    assert cfg.directory is not None
    assert cfg.directory.type == "meme"
    assert cfg.directory.timeout_s == 7.0
    assert cfg.directory.cache.exists_ttl_s == 900.0
    assert cfg.directory.cache.max_exists_entries == 5000
    # Defaults still apply for unset cache fields.
    assert cfg.directory.cache.pattern_ttl_s == 300.0
    assert cfg.directory.cache.negative_ttl_s == 60.0
